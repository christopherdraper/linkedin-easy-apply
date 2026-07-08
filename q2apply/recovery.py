"""Debug snapshots and stall recovery: cookie banners, corrupted fields, empty pages."""

import logging
import time
from typing import Dict, Optional

from job_search_apply import ApplicantProfile
from q2apply.analysis import _ai_answer_field
from q2apply.config import DEBUG_DIR
from q2apply.detection import _detect_submission_success
from q2apply.fields import _fill_field, _match_field_to_profile
from q2apply.logger import DecisionLogger

log = logging.getLogger(__name__)


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


# Imported after _save_debug_snapshot is defined so the verification, recovery,
# navigation import cycle resolves no matter which module loads first.
from q2apply.navigation import (  # noqa: E402
    _find_submit_button,
    _try_start_application_button,
)


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


def _handle_empty_page(page, job_id: str, logger: DecisionLogger) -> Optional[str]:
    """Handle pages with no form fields. Returns failure string or None (continue)."""
    # Check for submission success first -- confirmation pages often have minimal DOM
    if _detect_submission_success(page):
        logger.log(
            "submit",
            "confirmation page",
            reasoning="Detected submission success on page with minimal form content",
            confidence="high",
        )
        return "submitted"
    try:
        body_text = page.evaluate("document.body?.innerText?.toLowerCase()?.slice(0, 500) || ''")
    except Exception:
        body_text = ""
    if "saved a draft" in body_text or "saved draft" in body_text:
        logger.log(
            "abort",
            "draft saved",
            reasoning="Form auto-saved as draft (session timeout or validation error)",
            confidence="high",
        )
        _save_debug_snapshot(page, job_id, "draft_saved")
        return "failed: form saved as draft, needs manual resume"
    if _try_start_application_button(page, logger):
        return None  # continue loop
    logger.log("abort", "page", reasoning="Empty page snapshot", confidence="high")
    _save_debug_snapshot(page, job_id, "empty_snapshot")
    return "failed: empty page snapshot"
