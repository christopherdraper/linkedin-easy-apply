"""Deep-apply retry queue: re-queue Q1 failures for Q2 (MCP Playwright agent)
or Q3 (dashboard human escalation)."""

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

from jobapply import stats
from jobapply.applog import load_log
from jobapply.config import DATA_DIR, DEEP_APPLY_QUEUE_FILE, LOG_FILE
from jobapply.profile import ApplicantProfile

log = logging.getLogger(__name__)


# Failure categories eligible for retry via Q2/Q3
_DEEP_APPLY_ELIGIBLE_CATEGORIES = frozenset(
    {
        "form_stuck",
        "validation_error",
        "captcha",
        "no_apply_button",
        "login_wall",
        "modal_lost",
        "max_steps",
        "timeout",
        "unknown_error",
    }
)


def _load_deep_apply_queue() -> List[Dict]:
    if DEEP_APPLY_QUEUE_FILE.exists():
        try:
            return json.loads(DEEP_APPLY_QUEUE_FILE.read_text())
        except Exception:
            return []
    return []


def _save_deep_apply_queue(queue: List[Dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEEP_APPLY_QUEUE_FILE.write_text(json.dumps(queue, indent=2))


def _deep_apply_eligible(entry: Dict, already_queued_ids: set) -> bool:
    """Check if a failed application is eligible for Q2 retry."""
    if not entry.get("status", "").startswith("failed"):
        return False
    if (entry.get("match_score") or 0) < 0.7:
        return False
    if entry.get("failure_category") not in _DEEP_APPLY_ELIGIBLE_CATEGORIES:
        return False
    if entry.get("job_id") in already_queued_ids:
        return False
    return True


def _queue_for_deep_apply(app_entry: Dict, queue_tier: str = "q2") -> None:
    """Queue a failed application for Q2 (assisted) or Q3 (dashboard) retry."""
    queue = _load_deep_apply_queue()
    queue.append(
        {
            "job_id": app_entry["job_id"],
            "title": app_entry.get("title", ""),
            "company": app_entry.get("company", ""),
            "url": app_entry.get("url", ""),
            "ats_url": app_entry.get("ats_url", ""),
            "match_score": app_entry.get("match_score", 0),
            "failure_reason": app_entry.get("failure_category", ""),
            "original_status": app_entry.get("status", ""),
            "queued_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pre_computed": {
                "cover_letter_path": app_entry.get("cover_letter_path", ""),
                "field_answers": list(stats._field_fills),
                "scoring_reasoning": app_entry.get("reasoning", ""),
            },
            "status": "pending",
            "queue": queue_tier,
            "q2_attempts": 0,
            "decision_log": [],
            "deep_apply_status": None,
            "deep_apply_timestamp": None,
            "deep_apply_cost": None,
        }
    )
    _save_deep_apply_queue(queue)
    tier_label = "Q2 assisted" if queue_tier == "q2" else "Q3 dashboard"
    log.info(
        "   \U0001f4cb Queued for %s: %s - %s (score: %.2f)",
        tier_label,
        app_entry.get("company", "?"),
        app_entry.get("title", "?"),
        app_entry.get("match_score", 0),
    )


def _generate_deep_apply_prompt(queue_entry: Dict, profile: ApplicantProfile) -> str:
    """Generate a structured prompt for the Claude Chrome extension to complete an application."""
    pre = queue_entry.get("pre_computed", {})
    pct = int(queue_entry.get("match_score", 0) * 100)

    # Build field answers section (deduplicated)
    field_lines = []
    seen_fields: set = set()
    for fa in pre.get("field_answers", []):
        key = f"{fa['field'].lower().strip()}: {fa['value'].strip()}"
        if key in seen_fields:
            continue
        seen_fields.add(key)
        field_lines.append(f"- {fa['field']}: {fa['value']}")
    field_section = "\n".join(field_lines) if field_lines else "(no pre-filled answers available)"

    # Build screening answers section -- skip keys already covered in
    # field_answers above, and deduplicate exact key:value pairs
    screening_lines = []
    for k, v in profile.screening_answers.items():
        norm = f"{k.lower().strip()}: {v.strip().lower()}"
        if norm in seen_fields:
            continue
        seen_fields.add(norm)
        screening_lines.append(f"- {k}: {v}")
    screening_section = "\n".join(screening_lines) if screening_lines else "(none)"

    # Cover letter: inline the content so it can be pasted into Claude Desktop
    cover_text = pre.get("cover_letter_text", "")
    if not cover_text:
        cl_path = pre.get("cover_letter_path", "")
        if cl_path:
            try:
                cover_text = Path(cl_path).read_text().strip()
            except Exception:
                pass
    if cover_text:
        cover_section = (
            "If a cover letter field appears, paste the following cover letter:\n\n"
            f"---\n{cover_text}\n---"
        )
    else:
        cover_section = "No cover letter was generated for this application."

    # Resume filename only (user has it in ~/Downloads on their workstation)
    resume_name = Path(profile.resume_path).name if profile.resume_path else "resume.pdf"

    return f"""I need you to complete a job application. Follow these steps:

## Job Details
- Position: {queue_entry.get("title", "Unknown")} at {queue_entry.get("company", "Unknown")}
- Application URL: {queue_entry.get("url", "")}
- Match Score: {pct}%

## Step 1: Navigate
Open the application URL above.

## Step 2: Account/Login
If you see a login wall or account creation requirement:
- Create an account using: {profile.email}
- Generate a secure password
- If email verification is needed, open Gmail in a new tab, find the verification email, get the code, return and enter it

## Step 3: Fill the Application
Use these answers for form fields:

{field_section}

Additional screening answers (from profile):
{screening_section}

## Step 4: Resume
Upload my resume from: ~/Downloads/{resume_name}

## Step 5: Cover Letter
{cover_section}

## Step 6: Submit
Check any consent/terms checkboxes, then click Submit.

## If you encounter anything not covered above
Use your judgment to complete the application. Key facts:
- Authorized to work in US: Yes
- Requires sponsorship: No
- Willing to relocate: No
- Open to remote: Yes
- Years of experience: {profile.years_experience or 12}
- Current employer: {profile.current_employer or "N/A"}
- Current title: {profile.current_title or "N/A"}"""


def _mark_deep_apply_done(
    job_id: str,
    status: str,
    reason: Optional[str],
    decision_log: Optional[List[Dict]] = None,
) -> bool:
    """Mark a queue entry as done and update the application log."""
    queue = _load_deep_apply_queue()
    found = False
    for entry in queue:
        if entry["job_id"] == job_id:
            entry["status"] = "done"
            entry["deep_apply_status"] = status
            entry["deep_apply_timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if decision_log:
                entry["decision_log"] = decision_log
            found = True
            break
    if not found:
        return False
    _save_deep_apply_queue(queue)

    # Update the original application log entry
    all_apps = load_log() if LOG_FILE.exists() else []
    for app in all_apps:
        if app.get("job_id") == job_id:
            app["deep_apply_status"] = status
            app["deep_apply_timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if reason:
                app["deep_apply_reason"] = reason
            break
    if all_apps:
        LOG_FILE.write_text(json.dumps(all_apps, indent=2))

    return True


def _escalate_to_q3(job_id: str, reason: str) -> bool:
    """Escalate a Q2 entry to Q3 (dashboard) after assisted apply failure."""
    queue = _load_deep_apply_queue()
    for entry in queue:
        if entry["job_id"] == job_id:
            entry["queue"] = "q3"
            entry["status"] = "pending"
            entry["q2_attempts"] = entry.get("q2_attempts", 0)
            entry["escalation_reason"] = reason
            _save_deep_apply_queue(queue)
            log.info(
                "   Escalated to Q3 (dashboard): %s - %s (%s)",
                entry.get("company", "?"),
                entry.get("title", "?"),
                reason,
            )
            return True
    return False
