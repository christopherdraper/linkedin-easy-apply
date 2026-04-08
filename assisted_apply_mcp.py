#!/usr/bin/env python3
"""
Q2 Assisted Apply agent.

Uses Playwright (sync API) with Claude AI to autonomously complete job
applications that failed during Q1 automated batch runs.  Produces a
per-application decision log for human review.

Replaces deep_apply_computer_use.py (Xvfb + xdotool + screenshot vision).

Usage:
    python assisted_apply_mcp.py                     # process next pending Q2
    python assisted_apply_mcp.py --job-id li_abc      # process specific job
    python assisted_apply_mcp.py --list               # list Q2 pending
    python assisted_apply_mcp.py --max 5              # process up to 5 pending
"""

import argparse
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_search_apply import (  # noqa: E402
    ApplicantProfile,
    _attempt_account_creation,
    _attempt_ats_login,
    _detect_captcha,
    _ensure_logged_in,
    _escalate_to_q3,
    _fetch_verification_code_from_gmail,
    _get_domain,
    _load_deep_apply_queue,
    _mark_deep_apply_done,
    _playwright_context,
    _safe_click,
    _save_deep_apply_queue,
    _solve_captcha,
    _stealth_playwright,
)

log = logging.getLogger("assisted_apply")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

DATA_DIR = Path.home() / ".local" / "share" / "job-apply"
DEBUG_DIR = DATA_DIR / "debug"
PROFILE_PATH = DATA_DIR / "profile.json"

MODEL = "claude-sonnet-4-6"
MAX_PAGE_ATTEMPTS = 4
MAX_TOTAL_STEPS = 30


# ---------------------------------------------------------------------------
# Decision Logger
# ---------------------------------------------------------------------------


class DecisionLogger:
    """Accumulates a structured decision log for one application."""

    def __init__(self):
        self._entries: List[Dict] = []
        self._step = 0

    def log(
        self,
        action: str,
        target: str,
        value: str = "",
        reasoning: str = "",
        confidence: str = "high",
    ) -> None:
        self._step += 1
        entry = {
            "step": self._step,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "target": target,
            "value": value,
            "reasoning": reasoning,
            "confidence": confidence,
        }
        self._entries.append(entry)
        level = {"high": "INFO", "medium": "INFO", "uncertain": "WARNING"}.get(confidence, "INFO")
        getattr(log, level.lower())(
            "  [%d] %s %s -> %s (%s)", self._step, action, target, value or "-", confidence
        )

    def entries(self) -> List[Dict]:
        return list(self._entries)


# ---------------------------------------------------------------------------
# Profile-first field matching (deterministic, no AI needed)
# ---------------------------------------------------------------------------

# Map of common form label patterns -> profile attribute paths
_FIELD_MAP = {
    r"full.?name": "full_name",
    r"e.?mail": "email",
    r"phone|mobile|telephone": "phone",
    r"city": "city",
    r"\bstate\b|\bprovince\b": "state",
    r"zip|postal": "zip_code",
    r"country": "country",
    r"linkedin": "linkedin_url",
    r"github": "github_url",
}


def _match_field_to_profile(label: str, profile: ApplicantProfile) -> Optional[str]:
    """Try deterministic profile lookup before falling back to AI.

    Returns the value if matched, None if no match found.
    """
    label_lower = label.lower().strip()

    # Skip deterministic field matching for question-like labels (> 40 chars or
    # contains '?'). These need AI judgment, not pattern matching -- e.g.,
    # "What state will you work remotely from?" should NOT return the state abbrev.
    is_question = "?" in label_lower or len(label_lower) > 40

    field_map_matched = False

    if not is_question:
        # Profile fields first (name, email, phone, etc.)
        if profile.full_name:
            parts = profile.full_name.split(None, 1)
            if re.search(r"first.?name", label_lower) and parts:
                return parts[0]
            if re.search(r"last.?name", label_lower) and len(parts) > 1:
                return parts[1]

        for pattern, attr in _FIELD_MAP.items():
            if re.search(pattern, label_lower):
                val = getattr(profile, attr, None)
                if val:
                    return str(val)
                # Pattern matched but value is empty (e.g. github_url=None).
                # Don't fall through to screening answers which may return a
                # wrong-type value (e.g. "12" years for a URL field).
                field_map_matched = True
                break

    # Screening answers match (years of X, salary, etc.)
    # Skip if the field map already claimed this label (even with empty value).
    if not field_map_matched:
        for key, val in profile.screening_answers.items():
            if key.lower() in label_lower or label_lower in key.lower():
                return str(val)

    return None


# ---------------------------------------------------------------------------
# AI page analysis
# ---------------------------------------------------------------------------

_PAGE_ANALYSIS_SYSTEM = """You are an expert at completing online job application forms.
You will receive a JSON array of interactive form elements on the page. Each element has:
- "ref": element reference (e.g. "E1", "E23") - use this exact string in your response
- "tag": HTML tag (input, select, textarea, button)
- "type": input type (text, email, tel, file, checkbox, radio, submit)
- "label": the field label text
- "value": current value (already filled if non-empty)
- "text": button/element text content
- "required": whether the field is required
- "checked": whether checkbox/radio is checked
- "options": dropdown options (for select elements)

Return a JSON array of actions. Each action object must have:
- "action": one of "fill", "select", "click", "upload", "skip"
- "ref": the exact element reference from the snapshot (e.g. "E5")
- "target": human-readable description of the element
- "value": the value to fill/select (empty for click/skip)
- "reasoning": brief explanation of why
- "confidence": "high", "medium", or "uncertain"

Rules:
- Fill form fields with applicant profile data
- SKIP fields that already have the correct value (check the "value" field)
- For dropdowns/selects, pick the closest matching option from available choices
- For file inputs (type=file), use "upload" action
- For radio buttons / checkboxes, use "click" action on the correct option
- Do NOT include Submit/Next button clicks in your response - only fill form fields
- Skip fields you cannot determine a value for
- Return ONLY the JSON array, no other text
- Process fields in top-to-bottom order as they appear on the page"""


def _ai_analyze_page(
    snapshot: str,
    profile: ApplicantProfile,
    job_title: str,
    company: str,
) -> List[Dict]:
    """Use Claude to analyze a page snapshot and return planned actions."""
    client = anthropic.Anthropic()

    profile_summary = _build_profile_context(profile)

    prompt = f"""Applicant profile:
{profile_summary}

Applying for: {job_title} at {company}

Page accessibility snapshot:
{snapshot}

Analyze the form fields on this page and return a JSON array of actions to complete them."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=_PAGE_ANALYSIS_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    # Extract JSON array from response (may be wrapped in markdown code block)
    json_match = re.search(r"\[.*\]", text, re.DOTALL)
    if not json_match:
        log.warning("AI returned no valid JSON actions")
        return []

    try:
        actions = json.loads(json_match.group())
        return actions if isinstance(actions, list) else []
    except json.JSONDecodeError:
        log.warning("Failed to parse AI action JSON")
        return []


def _build_profile_context(profile: ApplicantProfile) -> str:
    """Build a concise profile summary for AI prompts."""
    lines = []
    for attr in [
        "full_name",
        "email",
        "phone",
        "city",
        "state",
        "zip_code",
        "country",
        "linkedin_url",
        "github_url",
        "current_title",
        "current_employer",
    ]:
        val = getattr(profile, attr, None)
        if val:
            lines.append(f"- {attr.replace('_', ' ').title()}: {val}")

    if profile.years_experience:
        lines.append(f"- Total years experience: {profile.years_experience}")

    if profile.screening_answers:
        lines.append("\nScreening answers:")
        for k, v in profile.screening_answers.items():
            lines.append(f"  - {k}: {v}")

    return "\n".join(lines)


def _ai_answer_field(
    label: str,
    field_type: str,
    options: List[str],
    profile: ApplicantProfile,
    job_title: str,
    company: str,
) -> Optional[str]:
    """Use Claude to answer a single form field that deterministic matching missed."""
    client = anthropic.Anthropic()

    options_str = ""
    if options:
        options_str = f"\nAvailable options: {', '.join(options)}"

    prompt = f"""Applicant profile:
{_build_profile_context(profile)}

Applying for: {job_title} at {company}

Field label: "{label}"
Field type: {field_type}{options_str}

Rules:
- "years of experience with X": single whole number
- Yes/No questions: just "Yes" or "No"
- For dropdowns, pick the exact option text from available options
- Output ONLY the value. No quotes, no units, no explanation."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=50,
        system="You fill job application forms. Output ONLY the answer value, nothing else.",
        messages=[{"role": "user", "content": prompt}],
    )
    answer = response.content[0].text.strip()
    return answer if answer else None


# ---------------------------------------------------------------------------
# Playwright execution helpers
# ---------------------------------------------------------------------------


def _get_accessibility_snapshot(page) -> str:
    """Get a text representation of the page accessibility tree."""
    try:
        snapshot = page.accessibility.snapshot()
        if not snapshot:
            return ""
        return json.dumps(snapshot, indent=2)
    except Exception as e:
        log.warning("Failed to get accessibility snapshot: %s", e)
        return ""


def _get_page_text_snapshot(page) -> str:
    """Get a simplified text snapshot of interactive form elements on the page."""
    return page.evaluate("""() => {
        const els = document.querySelectorAll(
            'input:not([type="hidden"]), select, textarea, ' +
            'button, [role="button"], [role="combobox"], ' +
            '[role="radio"], [role="checkbox"], [role="option"]'
        );
        const items = [];
        let refCounter = 0;
        for (const el of els) {
            if (el.offsetParent === null && el.getAttribute('type') !== 'file') continue;
            refCounter++;
            const ref = 'E' + refCounter;
            el.setAttribute('data-qa-ref', ref);
            const tag = el.tagName.toLowerCase();
            const type = el.getAttribute('type') || '';
            const role = el.getAttribute('role') || '';
            const name = el.getAttribute('name') || '';

            // Build label: check for associated <label>, aria-label, placeholder
            let label = el.getAttribute('aria-label') || '';
            if (!label) {
                const id = el.getAttribute('id');
                if (id) {
                    const labelEl = document.querySelector('label[for="' + id + '"]');
                    if (labelEl) label = labelEl.textContent.trim().substring(0, 100);
                }
            }
            if (!label) label = el.getAttribute('placeholder') || '';
            if (!label && el.closest('label')) {
                label = el.closest('label').textContent.trim().substring(0, 100);
            }

            const value = el.value || '';
            const text = el.textContent?.trim().substring(0, 100) || '';
            const required = el.hasAttribute('required') ||
                el.getAttribute('aria-required') === 'true';
            const checked = el.checked || false;

            let options = [];
            if (tag === 'select') {
                options = Array.from(el.options).map(o => o.text.trim());
            }

            items.push({
                ref, tag, type, role, name, label, value, text, required, checked,
                options: options.length > 0 ? options : undefined,
            });
        }
        return JSON.stringify(items);
    }""")


def _fill_react_select(page, el, ref: str, value: str) -> bool:
    """Fill a React Select / combobox component via Playwright + fiber fallback."""
    el_id = el.evaluate("e => (e.id || '').toLowerCase()")
    is_location = any(kw in el_id for kw in ("location", "city"))
    loc = page.locator(f'[data-qa-ref="{ref}"]')

    # Step 1: Try standard Playwright interaction
    loc.scroll_into_view_if_needed()
    page.wait_for_timeout(200)
    el.evaluate("e => { e.focus(); }")
    page.wait_for_timeout(200)
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(400)
    loc.press_sequentially(value, delay=60)
    page.wait_for_timeout(2500 if is_location else 1200)

    option_sel = (
        "[role='option']:not(.iti__country), [class*='select__option'], "
        "[class*='suggestion'], [class*='pac-item']"
    )
    option = page.query_selector(option_sel)
    if option:
        try:
            option.evaluate("e => e.click()")
        except Exception:
            page.keyboard.press("Enter")
        return True

    # Step 2: Fallback -- directly invoke React Select onChange via fiber tree
    ok = page.evaluate(
        """([ref, value]) => {
        const el = document.querySelector('[data-qa-ref="' + ref + '"]');
        if (!el) return false;
        const shell = el.closest('[class*="select-shell"]')
            || el.closest('[class*="select__container"]');
        if (!shell) return false;
        const fkey = Object.keys(shell).find(k =>
            k.startsWith('__reactFiber') ||
            k.startsWith('__reactInternalInstance'));
        if (!fkey) return false;
        let fiber = shell[fkey];
        for (let i = 0; i < 30 && fiber; i++) {
            const props = fiber.memoizedProps || fiber.pendingProps || {};
            if (typeof props.onChange === 'function' &&
                props.options !== undefined) {
                const match = (props.options || []).find(o =>
                    (o.label || '').toLowerCase().includes(value.toLowerCase()));
                if (match) { props.onChange(match); return true; }
                props.onChange({value: value, label: value});
                return true;
            }
            fiber = fiber.return;
        }
        return false;
    }""",
        [ref, value],
    )
    if ok:
        page.wait_for_timeout(300)
    else:
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(300)
        page.keyboard.press("Enter")
    return True


def _robust_click(el, page, timeout: int = 5000) -> None:
    """Click element with JS fallback for overlay/intercept issues (e.g. Lever ATS)."""
    try:
        el.click(timeout=timeout)
    except Exception:
        # Playwright click failed (overlay intercepts pointer events, timeout, etc.)
        # Fall back to JS click which bypasses hit-testing
        el.evaluate("e => e.click()")


def _fill_field(page, ref: str, value: str, field_type: str = "text") -> bool:
    """Fill a form field by its data-qa-ref attribute."""
    try:
        el = page.query_selector(f'[data-qa-ref="{ref}"]')
        if not el:
            log.warning("Element %s not found", ref)
            return False

        tag = el.evaluate("e => e.tagName.toLowerCase()")

        if tag == "select":
            el.select_option(label=value)
            return True

        input_type = el.evaluate("e => e.type || ''")
        role = el.evaluate("e => e.getAttribute('role') || ''")

        if input_type in ("checkbox", "radio"):
            if value.lower() in ("true", "yes", "1"):
                if not el.is_checked():
                    _robust_click(el, page)
            return True

        # React Select / combobox
        if role == "combobox" or el.evaluate(
            "e => e.classList.contains('select__input') || "
            "e.closest('[class*=\"select__\"]') !== null"
        ):
            return _fill_react_select(page, el, ref, value)

        if tag in ("input", "textarea"):
            # Location autocomplete fields need keystroke typing to trigger
            # Google Places / Greenhouse autocomplete dropdowns
            el_label = el.evaluate(
                "e => ((e.getAttribute('aria-label') || '') + ' ' + "
                "(e.getAttribute('name') || '') + ' ' + "
                "(e.getAttribute('placeholder') || '') + ' ' + "
                "(e.getAttribute('autocomplete') || '') + ' ' + "
                "(e.getAttribute('id') || '')).toLowerCase()"
            )
            is_autocomplete_field = any(
                kw in el_label
                for kw in ("location", "candidate-location", "address-input", "pac-input")
            ) or el.evaluate(
                "e => e.closest('[data-ui=\"address\"]') !== null || "
                "e.closest('[class*=\"address-autocomplete\"]') !== null || "
                "e.getAttribute('autocomplete') === 'address-line1'"
            )
            if is_autocomplete_field:
                _robust_click(el, page)
                el.fill("")
                page.keyboard.type(value, delay=50)
                page.wait_for_timeout(1500)
                suggestion = page.query_selector(
                    "[role='option'], [class*='suggestion'], "
                    "[class*='autocomplete'] li, [class*='listbox'] li, "
                    "[class*='pac-item'], [class*='dropdown-item'], "
                    "[class*='results'] li, [class*='menu'] [role='option']"
                )
                if suggestion:
                    try:
                        if suggestion.is_visible():
                            suggestion.evaluate("e => e.click()")
                            return True
                    except Exception:
                        pass
                page.keyboard.press("ArrowDown")
                page.keyboard.press("Enter")
                return True

            _robust_click(el, page)
            el.fill("")
            el.fill(value)
            return True

        # Fallback: try clicking (for buttons, etc.)
        _robust_click(el, page)
        return True

    except Exception as e:
        log.warning("Failed to fill %s: %s", ref, str(e)[:100])
        return False


def _click_element(page, ref: str) -> bool:
    """Click an element by its data-qa-ref attribute."""
    try:
        el = page.query_selector(f'[data-qa-ref="{ref}"]')
        if not el:
            log.warning("Element %s not found for click", ref)
            return False
        _robust_click(el, page)
        return True
    except Exception as e:
        log.warning("Failed to click %s: %s", ref, str(e)[:100])
        return False


def _upload_resume(page, ref: str, resume_path: str) -> bool:
    """Upload a file to a file input element."""
    try:
        el = page.query_selector(f'[data-qa-ref="{ref}"]')
        if not el:
            log.warning("File input %s not found", ref)
            return False
        el.set_input_files(resume_path)
        return True
    except Exception as e:
        log.warning("Failed to upload to %s: %s", ref, e)
        return False


def _detect_submission_success(page) -> bool:
    """Check if the page shows signs of successful submission."""
    try:
        body_text = page.evaluate("() => document.body.innerText").lower()
        success_phrases = [
            "application submitted",
            "thank you for applying",
            "thanks for applying",
            "application received",
            "successfully submitted",
            "your application has been",
            "we have received your application",
            "application complete",
        ]
        return any(phrase in body_text for phrase in success_phrases)
    except Exception:
        return False


def _detect_rejection(page) -> Optional[str]:
    """Check if the page shows rejection/block messages that mean we should stop."""
    try:
        body_text = page.evaluate("() => document.body.innerText").lower()
        rejection_phrases = {
            "flagged as possible spam": "spam filter",
            "flagged as spam": "spam filter",
            "couldn't submit your application": "submission blocked",
            "could not submit": "submission blocked",
            "already applied": "duplicate application",
            "you have already applied": "duplicate application",
            "position has been filled": "position closed",
            "no longer accepting": "position closed",
        }
        for phrase, reason in rejection_phrases.items():
            if phrase in body_text:
                return reason
        return None
    except Exception:
        return None


def _detect_email_verification(page) -> bool:
    """Check if the page is showing an email verification code prompt."""
    try:
        return page.evaluate("""() => {
            const text = (document.body ? document.body.innerText : '').toLowerCase();
            return text.includes('verification code was sent')
                || (text.includes('security code') && text.includes('character code'))
                || (text.includes('enter the') && text.includes('code to confirm'))
                || (text.includes('enter the 6') && text.includes('code'))
                || text.includes('verify your email');
        }""")
    except Exception:
        return False


def _handle_email_verification(page, profile: ApplicantProfile, logger: DecisionLogger) -> str:
    """Handle email verification code prompt. Returns status string.

    Fetches verification code via IMAP from Gmail, finds the input field,
    types the code, and submits.
    """
    if not getattr(profile, "gmail_app_password", None):
        logger.log(
            "abort", "verification", reasoning="No gmail_app_password in profile", confidence="high"
        )
        return "failed: email verification required but no gmail_app_password configured"

    logger.log(
        "navigate",
        "verification code page",
        reasoning="Fetching verification code via IMAP",
        confidence="high",
    )

    code = _fetch_verification_code_from_gmail(
        profile.email, profile.gmail_app_password, max_wait=45
    )
    if not code:
        logger.log(
            "abort",
            "verification",
            reasoning="Could not retrieve code from email",
            confidence="high",
        )
        return "failed: verification code not received from email"

    logger.log("fill_field", "verification code", code, "Code retrieved from email", "high")

    # Find the code input -- try multiple strategies
    code_input = None

    # Strategy 1: label-based locator
    for label_text in ("Security code", "Verification code", "Security Code"):
        try:
            loc = page.get_by_label(label_text)
            if loc.count() > 0 and loc.first.is_visible():
                code_input = loc.first
                break
        except Exception:
            continue

    # Strategy 2: attribute-based selector
    if not code_input:
        code_input = page.query_selector(
            "input[name*='security' i], input[name*='code' i], "
            "input[name*='verif' i], input[placeholder*='code' i], "
            "input[aria-label*='code' i], input[aria-label*='security' i]"
        )

    # Strategy 3: empty visible text input
    if not code_input:
        for inp in page.query_selector_all("input[type='text'], input:not([type])"):
            try:
                if inp.is_visible() and not inp.input_value():
                    code_input = inp
                    break
            except Exception:
                continue

    if not code_input:
        logger.log(
            "abort", "verification", reasoning="Could not find code input field", confidence="high"
        )
        return "failed: verification code input not found"

    # Type the code (React controlled components reject programmatic fill)
    code_input.click()
    code_input.evaluate("el => el.value = ''")
    code_input.type(code, delay=50)
    page.wait_for_timeout(500)

    # Submit
    submit_btn = page.query_selector(
        "button[type='submit'], input[type='submit'], button:has-text('Submit'), "
        "button:has-text('Verify'), button:has-text('Confirm')"
    )
    if submit_btn:
        try:
            submit_btn.click(timeout=5000)
        except Exception:
            submit_btn.evaluate("e => e.click()")
    else:
        page.keyboard.press("Enter")

    page.wait_for_timeout(5000)

    if _detect_submission_success(page):
        logger.log(
            "submit",
            "verification",
            reasoning="Submitted after verification code",
            confidence="high",
        )
        return "submitted"

    logger.log(
        "abort",
        "verification",
        reasoning="Verification code may have been rejected",
        confidence="uncertain",
    )
    return "failed: verification code rejected"


def _save_debug_snapshot(page, job_id: str, label: str) -> None:
    """Save a debug screenshot + HTML for troubleshooting."""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{job_id}_{label}_{ts}.png"))
    except Exception:
        pass
    try:
        html = page.content()
        (DEBUG_DIR / f"{job_id}_{label}_{ts}.html").write_text(html)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main application processing loop
# ---------------------------------------------------------------------------


def _retry_skipped_with_ai(
    action_plan: Dict,
    page,
    profile: ApplicantProfile,
    job_title: str,
    company: str,
    logger: DecisionLogger,
) -> None:
    """For skipped fields, check if the field is required or a textarea and try AI answer."""
    ref = action_plan.get("ref", "")
    target = action_plan.get("target", "")

    try:
        el = page.query_selector(f'[data-qa-ref="{ref}"]')
        if not el:
            logger.log("skip", target, "", action_plan.get("reasoning", ""), "medium")
            return

        # Check if field is required or is a textarea (likely essay question)
        field_info = el.evaluate("""e => ({
            tag: e.tagName.toLowerCase(),
            type: e.getAttribute('type') || '',
            required: e.hasAttribute('required') || e.getAttribute('aria-required') === 'true',
            value: e.value || '',
            options: e.tagName === 'SELECT' ? Array.from(e.options).map(o => o.text) : [],
        })""")

        is_textarea = field_info["tag"] == "textarea"
        is_required = field_info["required"]
        already_filled = bool(field_info["value"].strip())

        if already_filled or (not is_required and not is_textarea):
            logger.log("skip", target, "", action_plan.get("reasoning", ""), "high")
            return

        # Ask AI for an answer
        answer = _ai_answer_field(
            target, field_info["tag"], field_info["options"], profile, job_title, company
        )
        if answer:
            _fill_field(page, ref, answer)
            logger.log(
                "fill_field", target, answer[:80], "AI-generated answer for skipped field", "medium"
            )
        else:
            logger.log("skip", target, "", "AI could not generate answer", "uncertain")
    except Exception:
        logger.log("skip", target, "", action_plan.get("reasoning", ""), "medium")


def _execute_action_plan(
    action_plan: Dict,
    page,
    profile: ApplicantProfile,
    resume_path: str,
    job_title: str,
    company: str,
    logger: DecisionLogger,
) -> bool:
    """Execute a single AI-planned action. Returns True if a field was filled."""
    act = action_plan.get("action", "")
    ref = action_plan.get("ref", "")
    target = action_plan.get("target", "")
    value = action_plan.get("value", "")
    reasoning = action_plan.get("reasoning", "")
    confidence = action_plan.get("confidence", "medium")

    # Try deterministic profile match first for fill actions
    if act == "fill" and target:
        profile_val = _match_field_to_profile(target, profile)
        if profile_val:
            value = profile_val
            reasoning = f"Profile match: {target}"
            confidence = "high"

    if act == "fill":
        ok = _fill_field(page, ref, value)
        logger.log("fill_field", target, value, reasoning, confidence)
        return ok

    if act == "select":
        ok = _fill_field(page, ref, value, field_type="select")
        logger.log("select", target, value, reasoning, confidence)
        return ok

    if act == "click":
        ok = _click_element(page, ref)
        logger.log("click", target, value, reasoning, confidence)
        return ok

    if act == "upload":
        if resume_path and Path(resume_path).exists():
            _upload_resume(page, ref, resume_path)
            logger.log("upload", target, resume_path, "Uploading resume", "high")
        else:
            logger.log("skip", target, "", "No resume file available", "uncertain")
        return False

    # skip -- try AI answer for required/textarea fields before giving up
    if act == "skip":
        _retry_skipped_with_ai(action_plan, page, profile, job_title, company, logger)
        return False

    logger.log("skip", target, "", reasoning, confidence)
    return False


def _navigate_linkedin_to_ats(page, context, url, logger):
    """If on a LinkedIn job listing, find and click the external Apply button.

    Returns (page, status) where status is None on success or a failure/skip string.
    The returned page may be a new tab opened by the Apply button.
    """
    if "linkedin.com" not in page.url:
        return page, None

    _ensure_logged_in(page, url)
    page.wait_for_timeout(2000)

    apply_sel = (
        "a[aria-label*='Apply on company'], "
        "a[aria-label*='Apply on external'], "
        "button.jobs-apply-button:not(:has-text('Easy Apply')), "
        "a.jobs-apply-button, "
        "button:has-text('Apply'):not(:has-text('Easy'))"
    )

    apply_btn = None
    for _ in range(4):
        apply_btn = page.query_selector(apply_sel)
        if apply_btn:
            break
        page.wait_for_timeout(2000)

    if not apply_btn:
        # Check for "I'm interested" only (LinkedIn promoted ad, no external apply)
        interested_btn = page.query_selector(
            'button:has-text("I\u2019m interested"), button:has-text("I\'m interested")'
        )
        if interested_btn:
            logger.log(
                "abort",
                "LinkedIn listing",
                reasoning="Promoted ad with no apply button",
                confidence="high",
            )
            return page, "skipped: LinkedIn promoted ad (no apply button)"
        logger.log(
            "abort",
            "LinkedIn listing",
            reasoning="No external Apply button found",
            confidence="high",
        )
        return page, "failed: no Apply button found on LinkedIn job page"

    _safe_click(apply_btn, page)
    logger.log("click", "Apply button", reasoning="Navigate to external ATS", confidence="high")
    page.wait_for_timeout(3000)

    # Handle new tab (external URLs often open in new tab)
    if len(context.pages) > 1:
        page.close()
        page = context.pages[-1]
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:  # noqa: S110
            pass

    logger.log("navigate", page.url, reasoning="Arrived at external ATS", confidence="high")
    return page, None


def _find_submit_button(page) -> Optional[object]:
    """Find a Submit, Next, or Continue button on the page.

    Scrolls to the button if found off-screen.
    """
    selectors = [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Submit Application")',
        'button:has-text("Submit")',
        'button:has-text("Apply")',
        'button:has-text("Next")',
        'button:has-text("Continue")',
        '[role="button"]:has-text("Submit")',
        '[role="button"]:has-text("Next")',
    ]
    for sel in selectors:
        try:
            btn = page.query_selector(sel)
            if not btn:
                continue
            # Scroll into view first -- button may be below the fold
            try:
                btn.scroll_into_view_if_needed(timeout=2000)
                page.wait_for_timeout(300)
            except Exception:
                pass
            if btn.is_visible():
                return btn
        except Exception:
            continue
    return None


def _auto_upload_files(
    page, resume_path: str, cover_letter_path: str, logger: DecisionLogger
) -> None:
    """Find empty file inputs on the page and upload resume / cover letter."""
    if not resume_path or not Path(resume_path).exists():
        return
    for file_input in page.query_selector_all('input[type="file"]'):
        try:
            if file_input.evaluate("e => e.files && e.files.length > 0"):
                continue
            # Detect if this input is for a cover letter
            input_label = file_input.evaluate("""e => {
                let label = e.id || e.getAttribute('aria-label') || e.getAttribute('name') || '';
                const id = e.getAttribute('id');
                if (id) {
                    const lbl = document.querySelector('label[for="' + id + '"]');
                    if (lbl) label += ' ' + lbl.textContent;
                }
                if (e.closest('label')) label += ' ' + e.closest('label').textContent;
                // Also check sibling/parent text
                const parent = e.parentElement;
                if (parent) label += ' ' + (parent.textContent || '').substring(0, 200);
                return label.toLowerCase();
            }""")
            # Skip photo/image/avatar upload fields (they expect images, not PDFs)
            is_photo = any(
                kw in input_label
                for kw in ("photo", "avatar", "headshot", "picture", "image upload")
            )
            accept_attr = file_input.evaluate("e => e.getAttribute('accept') || ''")
            if is_photo or (accept_attr and "image" in accept_attr and "pdf" not in accept_attr):
                continue

            is_cover_letter = any(
                kw in input_label for kw in ("cover letter", "cover_letter", "coverletter")
            )
            if is_cover_letter and cover_letter_path and Path(cover_letter_path).exists():
                file_input.set_input_files(cover_letter_path)
                logger.log(
                    "upload", "cover letter", cover_letter_path, "Auto-upload cover letter", "high"
                )
            else:
                file_input.set_input_files(resume_path)
                display_label = file_input.evaluate(
                    "e => e.id || e.getAttribute('aria-label') || 'file'"
                )
                logger.log("upload", display_label, resume_path, "Auto-upload resume", "high")
        except Exception as e:
            log.debug("File upload failed: %s", e)


def _try_start_application_button(page, logger: DecisionLogger) -> bool:
    """Look for and click a 'Start Application' / 'Apply' / 'Application' tab on landing pages.

    Returns True if a button was found and clicked (caller should continue the loop).
    """
    # Try buttons first, then Workable-style "APPLICATION" tabs
    selectors = [
        'button:has-text("Start Application"), button:has-text("Apply Now")',
        'button:has-text("Apply for this"), a:has-text("Start Application")',
        'a:has-text("Apply Now"), button:has-text("Begin Application")',
        'a:has-text("Apply for this"), button:has-text("Apply")',
        '[role="button"]:has-text("Apply")',
        # Workable tab navigation
        'a:has-text("APPLICATION"), [role="tab"]:has-text("Application")',
        'button:has-text("APPLICATION")',
    ]
    for sel in selectors:
        btn = page.query_selector(sel)
        if btn:
            try:
                _robust_click(btn, page)
                label = btn.evaluate("e => e.textContent.trim().substring(0, 50)")
                logger.log(
                    "click",
                    label,
                    reasoning="No form fields visible, clicking start/apply/tab button",
                    confidence="medium",
                )
                page.wait_for_timeout(3000)
                return True
            except Exception:
                continue
    return False


def _handle_captcha(page, profile: ApplicantProfile, logger: DecisionLogger) -> bool:
    """Detect and solve CAPTCHA if present. Returns True if solved (caller should continue)."""
    captcha_info = _detect_captcha(page)
    if not captcha_info:
        return False

    api_key = getattr(profile, "captcha_api_key", None)
    if not api_key:
        logger.log(
            "abort",
            "CAPTCHA",
            reasoning="CAPTCHA detected but no captcha_api_key",
            confidence="high",
        )
        return False

    service = getattr(profile, "captcha_service", "2captcha")
    logger.log(
        "navigate",
        "CAPTCHA",
        reasoning=f"Solving {captcha_info.get('type', '?')} via {service}",
        confidence="medium",
    )
    solved = _solve_captcha(page, captcha_info, api_key, service)
    if solved:
        logger.log(
            "fill_field",
            "CAPTCHA token",
            reasoning="CAPTCHA solved, injected token",
            confidence="high",
        )
        page.wait_for_timeout(2000)
        return True
    logger.log("abort", "CAPTCHA", reasoning="CAPTCHA solve failed", confidence="high")
    return False


def _handle_login_wall(page, profile: ApplicantProfile, logger: DecisionLogger) -> bool:
    """Detect login/registration walls and handle via stored accounts or account creation.

    Returns True if login succeeded (caller should continue loop).
    """
    try:
        body = page.evaluate("document.body?.innerText?.toLowerCase()?.slice(0, 2000) || ''")
    except Exception:
        return False

    is_login_wall = any(
        phrase in body
        for phrase in (
            "create account",
            "sign in to apply",
            "log in to apply",
            "create an account",
            "sign up to apply",
            "register to apply",
        )
    ) and bool(page.query_selector("input[type='password'], input[type='email']"))

    if not is_login_wall:
        return False

    domain = _get_domain(page.url)
    logger.log(
        "navigate", "login wall", reasoning=f"Login wall detected on {domain}", confidence="high"
    )

    # Try existing account first
    if _attempt_ats_login(page, domain):
        logger.log(
            "click",
            "login",
            reasoning=f"Logged in with stored account on {domain}",
            confidence="high",
        )
        page.wait_for_timeout(3000)
        return True

    # Try creating a new account
    if _attempt_account_creation(page, profile):
        logger.log(
            "click", "account creation", reasoning=f"Created account on {domain}", confidence="high"
        )
        page.wait_for_timeout(3000)
        return True

    logger.log(
        "abort",
        "login wall",
        reasoning=f"Could not log in or create account on {domain}",
        confidence="high",
    )
    return False


def _handle_no_actions(page, logger: DecisionLogger) -> None:
    """When AI returns no form actions, try Submit/Next, then start/tab buttons."""
    submit_btn = _find_submit_button(page)
    if submit_btn:
        try:
            submit_btn.click()
            logger.log(
                "click",
                "Submit/Next button",
                reasoning="No fields to fill, advancing",
                confidence="high",
            )
            page.wait_for_timeout(3000)
            return
        except Exception:
            pass
    if _try_start_application_button(page, logger):
        return
    logger.log("skip", "page", reasoning="No actions and no submit button", confidence="uncertain")
    page.wait_for_timeout(2000)


def _clear_errored_uploads(page) -> None:
    """Remove errored file uploads (e.g. PDF uploaded to photo field)."""
    # Look for remove/X buttons near file upload error indicators
    for btn in page.query_selector_all('[data-ui="avatar"] ~ button, [data-ui="avatar"] button'):
        try:
            text = btn.text_content() or ""
            if "x" in text.lower() or "remove" in text.lower() or "clear" in text.lower():
                btn.click()
                page.wait_for_timeout(500)
        except Exception:
            continue
    # Generic: click X buttons inside file upload wrappers that have error indicators
    for wrapper in page.query_selector_all('[class*="error"], [class*="invalid"]'):
        try:
            close = wrapper.query_selector(
                'button[aria-label="Remove"], button[aria-label="Close"]'
            )
            if close:
                close.click()
                page.wait_for_timeout(500)
        except Exception:
            continue


def _dismiss_cookie_banner(page) -> bool:
    """Dismiss cookie consent banners that block page interaction."""
    selectors = [
        'button:has-text("Accept all")',
        'button:has-text("Accept All")',
        'button:has-text("Accept Cookies")',
        'button:has-text("I Accept")',
        'button:has-text("Got it")',
        'button:has-text("OK")',
        '[id*="cookie"] button:has-text("Accept")',
        '[class*="cookie"] button:has-text("Accept")',
        '[id*="consent"] button:has-text("Accept")',
    ]
    for sel in selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(1000)
                log.info("Dismissed cookie banner via: %s", sel)
                return True
        except Exception:
            continue
    return False


def _fix_corrupted_fields(page, profile: ApplicantProfile, logger: DecisionLogger) -> bool:
    """Detect and fix form fields with corrupted values (e.g. autocomplete contamination).

    Returns True if any field was fixed.
    """
    fixed = False
    for el in page.query_selector_all("input:not([type=hidden]):not([type=file])"):
        try:
            info = el.evaluate("""e => ({
                ref: e.getAttribute('data-qa-ref') || '',
                value: e.value || '',
                type: e.getAttribute('type') || 'text',
                label: (e.getAttribute('aria-label') || e.getAttribute('name') ||
                        e.getAttribute('placeholder') || '').toLowerCase(),
            })""")
            val = info["value"]
            # Corrupted: non-textarea input with >120 chars, or contains incrementally
            # repeated substrings (autocomplete contamination pattern)
            if info["type"] not in ("email", "url") and len(val) > 120:
                # Try to get the correct value from profile
                label = info["label"]
                correct = _match_field_to_profile(label, profile) if label else None
                if correct:
                    ref = info["ref"]
                    if ref:
                        _fill_field(page, ref, correct)
                        logger.log(
                            "fill_field",
                            label,
                            correct,
                            f"Fixed corrupted value ({len(val)} chars)",
                            "high",
                        )
                        fixed = True
        except Exception:
            continue
    return fixed


def _run_page_loop(page, profile, title, company, resume_path, cover_letter_path, job_id, logger):
    """Core form-filling loop. Returns a status string."""
    same_page_count = 0
    last_page_snapshot = ""

    # Dismiss any cookie/consent banners and clear errored uploads
    _dismiss_cookie_banner(page)
    _clear_errored_uploads(page)

    for _ in range(MAX_TOTAL_STEPS):
        if _detect_submission_success(page):
            logger.log(
                "submit",
                "confirmation page",
                reasoning="Detected submission success text",
                confidence="high",
            )
            return "submitted"

        # Handle email verification code (e.g. Greenhouse) before treating as rejection
        if _detect_email_verification(page):
            return _handle_email_verification(page, profile, logger)

        rejection = _detect_rejection(page)
        if rejection:
            logger.log(
                "abort",
                "page",
                reasoning=f"ATS rejected: {rejection}",
                confidence="high",
            )
            return f"failed: {rejection}"

        # CAPTCHA detection + solving via 2captcha/capsolver
        if _handle_captcha(page, profile, logger):
            continue

        # Login wall detection + account creation/login
        if _handle_login_wall(page, profile, logger):
            continue

        snapshot = _get_page_text_snapshot(page)
        if not snapshot or snapshot == "[]":
            # No form fields -- try clicking a "Start Application" button first
            if _try_start_application_button(page, logger):
                continue
            logger.log("abort", "page", reasoning="Empty page snapshot", confidence="high")
            _save_debug_snapshot(page, job_id, "empty_snapshot")
            return "failed: empty page snapshot"

        # Fix corrupted values from prior attempts (no-op after first fix)
        _fix_corrupted_fields(page, profile, logger)

        # Detect stall by comparing snapshot content (not URL, since SPA URLs don't change)
        if snapshot == last_page_snapshot:
            same_page_count += 1
            if same_page_count >= MAX_PAGE_ATTEMPTS:
                reason = f"stuck on same page state after {MAX_PAGE_ATTEMPTS} attempts"
                logger.log("abort", "page", reasoning=reason, confidence="high")
                _save_debug_snapshot(page, job_id, "stalled")
                return f"failed: {reason}"
        else:
            same_page_count = 0
        last_page_snapshot = snapshot

        # AI analysis: what fields need filling?
        actions = _ai_analyze_page(snapshot, profile, title, company)
        if not actions:
            _handle_no_actions(page, logger)
            continue

        # Execute fill/click actions (AI no longer returns submit clicks)
        for action_plan in actions:
            _execute_action_plan(action_plan, page, profile, resume_path, title, company, logger)

        _auto_upload_files(page, resume_path, cover_letter_path, logger)

        page.wait_for_timeout(1500)

        # After filling, click Submit/Next
        submit_btn = _find_submit_button(page)
        if submit_btn:
            try:
                submit_btn.click()
                logger.log(
                    "click",
                    "Submit/Next button",
                    reasoning="Fields filled, advancing form",
                    confidence="high",
                )
            except Exception:
                pass
            page.wait_for_timeout(3000)

    reason = f"exceeded {MAX_TOTAL_STEPS} total steps"
    logger.log("abort", "page", reasoning=reason, confidence="high")
    _save_debug_snapshot(page, job_id, "max_steps")
    return f"failed: {reason}"


def _clear_domain_cookies(context, url: str) -> None:
    """Clear cookies for the domain of the given URL to get a fresh form state.

    Never clears LinkedIn cookies (needed for session auth).
    """
    try:
        from urllib.parse import urlparse

        domain = urlparse(url).hostname or ""
        parts = domain.split(".")
        parent = ".".join(parts[-2:]) if len(parts) > 2 else domain

        # Safety: never clear LinkedIn session cookies
        if "linkedin.com" in parent:
            return

        cookies = context.cookies()
        to_clear = [c for c in cookies if parent in (c.get("domain", ""))]
        if to_clear:
            context.clear_cookies()
            keep = [c for c in cookies if parent not in (c.get("domain", ""))]
            if keep:
                context.add_cookies(keep)
            log.info("Cleared %d cookies for domain %s", len(to_clear), parent)
    except Exception as e:
        log.warning("Failed to clear domain cookies: %s", e)


def process_application(
    queue_entry: Dict,
    profile: ApplicantProfile,
    proxy: Optional[str] = None,
) -> str:
    """Navigate to a job application URL and attempt to complete it.

    Returns a status string: "submitted", "failed: <reason>", or "escalated: <reason>".
    """
    job_id = queue_entry["job_id"]
    url = queue_entry["url"]
    title = queue_entry.get("title", "Unknown")
    company = queue_entry.get("company", "Unknown")
    resume_path = getattr(profile, "resume_path", "")
    cover_letter_path = (queue_entry.get("pre_computed") or {}).get("cover_letter_path", "")
    logger = DecisionLogger()
    is_retry = queue_entry.get("q2_attempts", 0) > 0

    log.info("Processing: %s at %s (%s)", title, company, url)
    logger.log("navigate", url, reasoning="Starting application", confidence="high")

    with _stealth_playwright() as p:
        browser, context, page, owns_browser = _playwright_context(p, proxy=proxy)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

            # If URL is a LinkedIn listing, navigate to the external ATS first
            page, nav_status = _navigate_linkedin_to_ats(page, context, url, logger)
            if nav_status:
                return nav_status

            # Clear ATS domain cookies on retries (after ATS navigation)
            if is_retry:
                ats_url = page.url
                if ats_url != url:
                    _clear_domain_cookies(context, ats_url)
                    page.reload(wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3000)

            return _run_page_loop(
                page, profile, title, company, resume_path, cover_letter_path, job_id, logger
            )

        except Exception as e:
            reason = f"exception: {type(e).__name__}: {str(e)[:200]}"
            logger.log("abort", "page", reasoning=reason, confidence="high")
            _save_debug_snapshot(page, job_id, "exception")
            return f"failed: {reason}"

        finally:
            # Always persist the decision log back to the queue entry
            queue = _load_deep_apply_queue()
            for entry in queue:
                if entry["job_id"] == job_id:
                    entry["decision_log"] = logger.entries()
                    entry["q2_attempts"] = entry.get("q2_attempts", 0) + 1
                    break
            _save_deep_apply_queue(queue)

            try:
                page.close()
                if owns_browser:
                    browser.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Queue processing
# ---------------------------------------------------------------------------


def _get_q2_pending() -> List[Dict]:
    """Get all pending Q2 entries from the queue."""
    queue = _load_deep_apply_queue()
    return [q for q in queue if q.get("status") == "pending" and q.get("queue", "q2") == "q2"]


def process_next_pending(
    proxy: Optional[str] = None,
    max_count: int = 1,
) -> List[Dict]:
    """Process up to max_count pending Q2 entries. Returns results list."""
    profile = ApplicantProfile.from_dict(json.loads(PROFILE_PATH.read_text()))
    pending = _get_q2_pending()

    if not pending:
        log.info("No pending Q2 entries in queue.")
        return []

    results = []
    for entry in pending[:max_count]:
        job_id = entry["job_id"]

        # Mark as in_progress
        queue = _load_deep_apply_queue()
        for q in queue:
            if q["job_id"] == job_id:
                q["status"] = "in_progress"
                break
        _save_deep_apply_queue(queue)

        status = process_application(entry, profile, proxy=proxy)
        log.info("Result for %s: %s", job_id, status)

        if status == "submitted":
            _mark_deep_apply_done(job_id, "submitted", None)
        elif status.startswith("failed"):
            # Check if we should escalate to Q3
            queue = _load_deep_apply_queue()
            q_entry = next((q for q in queue if q["job_id"] == job_id), None)
            attempts = q_entry.get("q2_attempts", 0) if q_entry else 0

            if attempts >= 2:
                _escalate_to_q3(job_id, status.replace("failed: ", ""))
                status = f"escalated: {status}"
            else:
                # Reset to pending for another Q2 attempt
                for q in queue:
                    if q["job_id"] == job_id:
                        q["status"] = "pending"
                        break
                _save_deep_apply_queue(queue)

        results.append({"job_id": job_id, "status": status})

    return results


def process_by_id(
    job_id: str,
    proxy: Optional[str] = None,
) -> str:
    """Process a specific job by ID. Returns status string."""
    profile = ApplicantProfile.from_dict(json.loads(PROFILE_PATH.read_text()))
    queue = _load_deep_apply_queue()
    entry = next((q for q in queue if q["job_id"] == job_id), None)

    if not entry:
        return f"Job ID '{job_id}' not found in queue"

    # Mark as in_progress
    entry["status"] = "in_progress"
    _save_deep_apply_queue(queue)

    status = process_application(entry, profile, proxy=proxy)
    log.info("Result for %s: %s", job_id, status)

    if status == "submitted":
        _mark_deep_apply_done(job_id, "submitted", None)
    elif status.startswith("failed"):
        queue = _load_deep_apply_queue()
        q_entry = next((q for q in queue if q["job_id"] == job_id), None)
        attempts = q_entry.get("q2_attempts", 0) if q_entry else 0
        if attempts >= 2:
            _escalate_to_q3(job_id, status.replace("failed: ", ""))
        else:
            for q in queue:
                if q["job_id"] == job_id:
                    q["status"] = "pending"
                    break
            _save_deep_apply_queue(queue)

    return status


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Q2 Assisted Apply (MCP Playwright)")
    parser.add_argument("--job-id", help="Process a specific job by ID")
    parser.add_argument("--list", action="store_true", help="List pending Q2 entries")
    parser.add_argument("--max", type=int, default=1, help="Max entries to process (default: 1)")
    parser.add_argument("--proxy", help="Proxy server URL")
    args = parser.parse_args()

    if args.list:
        pending = _get_q2_pending()
        if not pending:
            print("No pending Q2 entries.")
            return
        print(f"\n{'=' * 80}")
        print(f"  Q2 Pending: {len(pending)} entries")
        print(f"{'=' * 80}\n")
        for q in pending:
            score_pct = int((q.get("match_score") or 0) * 100)
            attempts = q.get("q2_attempts", 0)
            print(
                f"  [{q['job_id']}] {q.get('company', '?')} - {q.get('title', '?')}"
                f"  (score: {score_pct}%, attempts: {attempts})"
            )
        return

    if args.job_id:
        status = process_by_id(args.job_id, proxy=args.proxy)
        print(f"Result: {status}")
        return

    results = process_next_pending(proxy=args.proxy, max_count=args.max)
    if not results:
        print("No pending Q2 entries to process.")
        return

    print(f"\nProcessed {len(results)} entries:")
    for r in results:
        print(f"  [{r['job_id']}] {r['status']}")


if __name__ == "__main__":
    main()
