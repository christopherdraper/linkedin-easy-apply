"""The batch auto-apply workflow: search a source, score jobs, generate cover
letters, submit via Easy Apply or external ATS, and log the results."""

import logging
import re
import time
from typing import Dict, Optional

from jobapply import stats
from jobapply.ai import _AI_AVAILABLE
from jobapply.applog import already_applied, load_log, save_log
from jobapply.browser import _human_delay
from jobapply.config import COVER_LETTER_DIR, LOG_FILE
from jobapply.content import (
    _save_cover_letter_docx,
    ai_build_notes,
    ai_generate_cover_letter,
    ai_score_job,
)
from jobapply.easy_apply import submit_easy_apply
from jobapply.external import submit_external_apply
from jobapply.outreach import _message_hiring_manager_after_apply
from jobapply.profile import ApplicantProfile, JobSearchParams
from jobapply.queue import (
    _deep_apply_eligible,
    _load_deep_apply_queue,
    _queue_for_deep_apply,
)
from jobapply.search import (
    search_biotech,
    search_hn_whos_hiring,
    search_linkedin,
    search_remoteok,
)
from jobapply.stats import _categorize_failure, _compute_cost_usd, _detect_ats_platform

log = logging.getLogger(__name__)


def auto_apply_workflow(  # noqa: C901
    params: JobSearchParams,
    profile: ApplicantProfile,
    max_applications: int = 10,
    min_match_score: float = 0.30,
    dry_run: bool = False,
    proxy: Optional[str] = None,
    source: str = "linkedin",
) -> Dict:
    source_labels = {
        "linkedin": "LinkedIn",
        "remoteok": "RemoteOK",
        "hn": "HackerNews Who's Hiring",
        "biotech": "Biotech/Pharma Careers",
    }
    label = source_labels.get(source, source)
    log.info(f"🚀 {label} job apply workflow (Easy Apply + External)")
    log.info(f"   dry_run={dry_run}  max={max_applications}  min_score={min_match_score}")
    log.info(f"   AI: {'enabled' if _AI_AVAILABLE else 'disabled (install anthropic)'}\n")

    applied_urls = already_applied(load_log())
    if applied_urls:
        log.info(f"   Skipping {len(applied_urls)} already-applied jobs\n")

    log.info(f"🔍 Searching {label} for '{params.title}'...")
    try:
        if source == "remoteok":
            jobs = search_remoteok(params)
        elif source == "hn":
            jobs = search_hn_whos_hiring(params)
        elif source == "biotech":
            jobs = search_biotech(params)
        else:
            jobs = search_linkedin(params, proxy=proxy)
    except RuntimeError as e:
        log.error(f"❌ Search failed: {e}")
        return {"applications": [], "total": 0, "jobs_found": 0}

    easy_count = sum(1 for j in jobs if j.get("apply_type") == "easy_apply")
    ext_count = sum(1 for j in jobs if j.get("apply_type") == "external")
    log.info(f"✅ {len(jobs)} jobs found ({easy_count} Easy Apply, {ext_count} external)\n")
    if not jobs:
        return {"applications": [], "total": 0, "jobs_found": 0}

    applications = []
    applied = 0

    for job in jobs:
        if applied >= max_applications:
            log.info(f"✋ Reached limit ({max_applications})")
            break

        canonical_url = re.sub(r"\?.*$", "", job["url"]) if job.get("url") else ""
        if (
            job["url"] in applied_urls
            or canonical_url in applied_urls
            or job.get("id") in applied_urls
        ):
            log.info(f"⏭  Already applied: {job['title']} at {job['company']}")
            continue

        compat = ai_score_job(job, profile)

        if compat["match_score"] < min_match_score:
            reason = f" — {compat['reasoning']}" if compat.get("reasoning") else ""
            log.info(
                f"⏭  Low score ({compat['match_score']}): {job['title']} at {job['company']}{reason}"
            )
            continue

        if compat.get("deal_breakers"):
            log.info(
                f"⏭  Deal-breakers: {job['title']} at {job['company']} → {compat['deal_breakers']}"
            )
            continue

        log.info(f"✨ {job['title']} at {job['company']} (score {compat['match_score']})")
        if compat.get("reasoning"):
            log.info(f"   {compat['reasoning']}")
        if compat.get("matched_skills"):
            log.info(f"   Skills: {', '.join(compat['matched_skills'][:6])}")

        # Generate AI cover letter
        COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
        cover_letter = ai_generate_cover_letter(job, profile)
        cl_file = _save_cover_letter_docx(cover_letter, job["id"])

        apply_type = job.get("apply_type", "easy_apply")

        if dry_run:
            log.info(f"   ⚠️  Dry run — not submitted ({apply_type})")
            status = "dry_run"
        elif apply_type == "easy_apply":
            log.info("   Submitting via Easy Apply...")
            status = submit_easy_apply(job, profile, proxy=proxy)
            icon = (
                "✅"
                if status == "submitted"
                else "🛡️ "
                if status.startswith("aborted")
                else "⚠️ "
                if "unconfirmed" in status
                else "❌"
            )
            log.info(f"   {icon} {status}")
        else:
            log.info("   Submitting via external apply...")
            status = submit_external_apply(
                job,
                profile,
                cover_letter_path=str(cl_file),
                proxy=proxy,
                dry_run=dry_run,
            )
            icon = (
                "✅"
                if status == "submitted"
                else "🔒"
                if "requires account" in status
                else "🛡️ "
                if status.startswith("aborted")
                else "⚠️ "
                if "unconfirmed" in status
                else "❌"
            )
            log.info(f"   {icon} {status}")

        # AI-generated notes for the log
        notes = ai_build_notes(job, compat)

        # Message hiring manager after successful application
        msg_status = None
        msg_text = None
        msg_poster = None
        if status.startswith("submitted") and profile.message_hiring_manager:
            try:
                msg_status, msg_text, msg_poster = _message_hiring_manager_after_apply(
                    job, profile, proxy=proxy
                )
                if msg_status:
                    log.info(f"   📨 Hiring manager message: {msg_status}")
            except Exception as exc:
                log.info("   ⚠️  Hiring manager message failed: %s", exc)

        applications.append(
            {
                "job_id": job["id"],
                "title": job["title"],
                "company": job["company"],
                "url": job["url"],
                "ats_url": stats._final_ats_url or "",
                "location": job.get("location", ""),
                "status": status,
                "apply_type": job.get("apply_type", "easy_apply"),
                "posted_ago": job.get("posted_ago", ""),
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "match_score": compat["match_score"],
                "reasoning": compat.get("reasoning", ""),
                "deal_breakers": compat.get("deal_breakers", []),
                "cover_letter_path": str(cl_file),
                "notes": notes,
                "hiring_manager_messaged": msg_status,
                "hiring_manager_message_text": msg_text,
                "hiring_manager_name": msg_poster,
                "fields_filled": list(stats._field_fills),
                "ai_answer_failures": list(stats._ai_answer_failures),
                "ai_tokens": {"input": stats._ai_tokens_in, "output": stats._ai_tokens_out},
                "cost_usd": _compute_cost_usd(stats._ai_tokens_in, stats._ai_tokens_out),
                "duration_seconds": round(time.time() - stats._apply_start_time, 1)
                if stats._apply_start_time
                else None,
                "ats_platform": _detect_ats_platform(stats._final_ats_url or job.get("url", "")),
                "failure_category": _categorize_failure(status)
                if status.startswith("failed")
                else None,
            }
        )
        # Deep-apply queue: check if failed high-match app should be re-queued
        if status.startswith("failed"):
            _queued_ids = {q["job_id"] for q in _load_deep_apply_queue()}
            app_record = applications[-1]
            if _deep_apply_eligible(app_record, _queued_ids):
                _queue_for_deep_apply(app_record)
        applied += 1
        # Human-like delay between applications (longer for Easy Apply to avoid spam flags)
        if job.get("easy_apply"):
            _human_delay(base=8.0, jitter=12.0)  # 8-20s, occasionally 20-35s
        else:
            _human_delay(base=3.0, jitter=5.0)  # 3-8s for external ATS

    save_log(applications)

    log.info("\n" + "=" * 50)
    log.info("📊 SUMMARY")
    log.info(f"   Jobs found:    {len(jobs)}")
    submitted = sum(1 for a in applications if a["status"].startswith("submitted"))
    log.info(f"   Submitted:     {submitted}")
    skipped_account = sum(1 for a in applications if a["status"] == "skipped: requires account")
    if skipped_account:
        log.info(f"   Skipped (login): {skipped_account}")
    aborted = sum(1 for a in applications if a["status"].startswith("aborted"))
    if aborted:
        log.info(f"   Aborted:       {aborted} (injection/suspicious fields detected)")
    failed = sum(1 for a in applications if a["status"].startswith("failed"))
    if failed:
        log.info(f"   Failed:        {failed}")
    log.info(f"   Log:           {LOG_FILE}")
    log.info("=" * 50)

    return {"applications": applications, "total": submitted, "jobs_found": len(jobs)}
