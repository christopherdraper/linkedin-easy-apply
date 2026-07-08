"""Core Q2 state machine: action execution, the page loop, application processing."""

import logging
from pathlib import Path
from typing import Dict, Optional

from ats_handlers import get_handler
from job_search_apply import (
    ApplicantProfile,
    _load_deep_apply_queue,
    _playwright_context,
    _save_deep_apply_queue,
    _stealth_playwright,
)
from q2apply.analysis import _ai_analyze_page, _get_page_text_snapshot
from q2apply.config import MAX_PAGE_ATTEMPTS, MAX_TOTAL_STEPS
from q2apply.detection import (
    _detect_email_verification,
    _detect_rejection,
    _detect_submission_success,
)
from q2apply.fields import _click_element, _fill_field, _match_field_to_profile, _upload_resume
from q2apply.logger import DecisionLogger
from q2apply.navigation import (
    _auto_upload_files,
    _find_submit_button,
    _handle_captcha,
    _handle_login_wall,
    _navigate_linkedin_to_ats,
)
from q2apply.recovery import (
    _clear_errored_uploads,
    _dismiss_cookie_banner,
    _fix_corrupted_fields,
    _handle_empty_page,
    _handle_no_actions,
    _retry_skipped_with_ai,
    _save_debug_snapshot,
)
from q2apply.verification import _handle_email_verification

log = logging.getLogger(__name__)


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


def _run_page_loop(  # noqa: C901
    page,
    profile,
    title,
    company,
    resume_path,
    cover_letter_path,
    job_id,
    logger,
    handler=None,
    handler_ctx=None,
):
    """Core form-filling loop. Returns a status string."""
    same_page_count = 0
    last_page_snapshot = ""
    captcha_attempts = 0

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
            verify_result = _handle_email_verification(page, profile, logger)
            if verify_result != "continue":
                return verify_result
            same_page_count = 0
            last_page_snapshot = ""
            continue

        rejection = _detect_rejection(page)
        if rejection:
            logger.log(
                "abort",
                "page",
                reasoning=f"ATS rejected: {rejection}",
                confidence="high",
            )
            return f"failed: {rejection}"

        # CAPTCHA detection + solving (max 2 attempts to avoid burning credits)
        if captcha_attempts < 2 and _handle_captcha(page, profile, logger):
            captcha_attempts += 1
            continue

        # Handler: Q2 login wall resolution
        if handler and handler_ctx and handler.q2_resolve_login_wall(page, handler_ctx):
            continue

        # Login wall detection + account creation/login
        if _handle_login_wall(page, profile, logger):
            continue

        snapshot = _get_page_text_snapshot(page)
        if not snapshot or snapshot == "[]":
            empty_result = _handle_empty_page(page, job_id, logger)
            if empty_result:
                return empty_result
            continue

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
    url = queue_entry.get("ats_url") or queue_entry["url"]
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

            handler = get_handler(page.url)
            handler_ctx = {
                "profile": profile,
                "logger": logger,
                "job_id": job_id,
                "title": title,
                "company": company,
            }

            # Handler: Q2 pre-flight
            q2_abort = handler.q2_pre_flight(page, handler_ctx)
            if q2_abort:
                logger.log("abort", "pre_flight", reasoning=q2_abort, confidence="high")
                return q2_abort

            return _run_page_loop(
                page,
                profile,
                title,
                company,
                resume_path,
                cover_letter_path,
                job_id,
                logger,
                handler=handler,
                handler_ctx=handler_ctx,
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
