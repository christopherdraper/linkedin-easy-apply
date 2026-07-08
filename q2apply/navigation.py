"""ATS navigation: LinkedIn handoff, submit buttons, uploads, captchas, login walls."""

import logging
from pathlib import Path
from typing import Optional

from job_search_apply import (
    ApplicantProfile,
    _attempt_account_creation,
    _attempt_ats_login,
    _detect_captcha,
    _ensure_logged_in,
    _get_domain,
    _safe_click,
    _solve_captcha,
)
from q2apply.fields import _robust_click
from q2apply.logger import DecisionLogger

log = logging.getLogger(__name__)


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

    Prefers specific advance buttons (Submit/Next/Continue) over generic
    type=submit to avoid hitting "Save and exit" or "Save draft" buttons.
    Scrolls to the button if found off-screen.
    """
    # Text-specific selectors first (avoids "Save and exit" type=submit)
    selectors = [
        'button:has-text("Submit Application")',
        'button:has-text("Save and continue")',
        'button:has-text("Submit")',
        'button:has-text("Apply")',
        'button:has-text("Next")',
        'button:has-text("Continue")',
        '[role="button"]:has-text("Submit")',
        '[role="button"]:has-text("Next")',
        'input[type="submit"]',
        'button[type="submit"]',
    ]
    # Skip buttons that would save/exit/discard instead of advancing
    skip_texts = ("save and exit", "save draft", "discard", "cancel")
    for sel in selectors:
        try:
            btn = page.query_selector(sel)
            if not btn:
                continue
            btn_text = (btn.text_content() or "").strip().lower()
            if btn_text in skip_texts:
                continue
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
                from job_search_apply import _ensure_cover_letter_docx

                cover_letter_path = _ensure_cover_letter_docx(cover_letter_path)
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
        # Try clicking submit after CAPTCHA solve to advance past the challenge
        submit_btn = _find_submit_button(page)
        if submit_btn:
            try:
                _robust_click(submit_btn, page)
                page.wait_for_timeout(3000)
            except Exception:
                pass
        return True
    logger.log("abort", "CAPTCHA", reasoning="CAPTCHA solve failed", confidence="high")
    return False


# Imported after the submit and start-button helpers are defined so the
# verification, recovery, navigation import cycle resolves no matter which
# module loads first.
from q2apply.verification import _handle_identity_verification  # noqa: E402


def _handle_login_wall(page, profile: ApplicantProfile, logger: DecisionLogger) -> bool:
    """Detect login/registration walls and handle via stored accounts or account creation.

    Returns True if login succeeded (caller should continue loop).
    """
    try:
        body = page.evaluate("document.body?.innerText?.toLowerCase()?.slice(0, 2000) || ''")
    except Exception:
        return False

    # Check for PageUp-style identity verification first
    if "verify your identity" in body and "emailed one-time code" in body:
        return _handle_identity_verification(page, profile, logger)

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
