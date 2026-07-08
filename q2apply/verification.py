"""Email and identity verification flows (Greenhouse codes, PageUp OTP)."""

import logging
from typing import Optional

from job_search_apply import ApplicantProfile, _fetch_verification_code_from_gmail
from q2apply.detection import _detect_email_verification, _detect_submission_success
from q2apply.logger import DecisionLogger

log = logging.getLogger(__name__)


def _find_otp_button(page) -> Optional[object]:
    """Find the 'Login with emailed one-time code' button on identity verification pages."""
    btn = page.query_selector(
        'button:has-text("emailed one-time code"), '
        'a:has-text("emailed one-time code"), '
        '[role="button"]:has-text("emailed one-time code")'
    )
    if btn:
        return btn
    for el in page.query_selector_all("button, a, [role='button']"):
        try:
            txt = el.text_content() or ""
            if "one-time code" in txt.lower():
                return el
        except Exception:
            continue
    return None


def _find_code_input(page) -> Optional[object]:
    """Find an OTP/verification code input field on the page."""
    code_input = page.query_selector(
        "input[name*='code' i], input[name*='otp' i], input[name*='token' i], "
        "input[placeholder*='code' i], input[aria-label*='code' i]"
    )
    if code_input:
        return code_input
    for inp in page.query_selector_all(
        "input[type='text'], input:not([type]), input[type='number']"
    ):
        try:
            if inp.is_visible() and not inp.input_value():
                return inp
        except Exception:
            continue
    return None


def _handle_identity_verification(page, profile: ApplicantProfile, logger: DecisionLogger) -> bool:
    """Detect PageUp-style identity verification pages and handle via emailed OTP.

    These pages show "Verify your identity through:" with options including
    "Login with emailed one-time code". Returns True if verification succeeded.
    """
    try:
        body = page.evaluate("document.body?.innerText?.toLowerCase()?.slice(0, 2000) || ''")
    except Exception:
        return False

    if "verify your identity" not in body or "emailed one-time code" not in body:
        return False

    logger.log(
        "navigate",
        "identity verification page",
        reasoning="PageUp identity verification detected, using email OTP",
        confidence="high",
    )

    if not getattr(profile, "gmail_app_password", None):
        logger.log(
            "abort",
            "identity verification",
            reasoning="No gmail_app_password in profile",
            confidence="high",
        )
        return False

    otp_btn = _find_otp_button(page)
    if not otp_btn:
        logger.log(
            "abort",
            "identity verification",
            reasoning="Could not find 'emailed one-time code' button",
            confidence="high",
        )
        return False

    otp_btn.click()
    logger.log(
        "click",
        "Login with emailed one-time code",
        reasoning="Requesting OTP email from PageUp",
        confidence="high",
    )
    page.wait_for_timeout(5000)

    # PageUp can take 2-7 min to deliver OTP emails; use longer timeout
    code = _fetch_verification_code_from_gmail(
        profile.email, profile.gmail_app_password, max_wait=480
    )
    if not code:
        logger.log(
            "abort",
            "identity verification",
            reasoning="OTP code not received from email after 8 min",
            confidence="high",
        )
        return False

    logger.log("fill_field", "OTP code", code, "Code retrieved from email", "high")

    code_input = _find_code_input(page)
    if not code_input:
        logger.log(
            "abort",
            "identity verification",
            reasoning="Could not find OTP input field",
            confidence="high",
        )
        return False

    code_input.click()
    code_input.evaluate("el => el.value = ''")
    code_input.type(code, delay=50)
    page.wait_for_timeout(500)

    submit_btn = page.query_selector(
        "button[type='submit'], input[type='submit'], "
        "button:has-text('Verify'), button:has-text('Continue'), "
        "button:has-text('Submit'), button:has-text('Log in')"
    )
    if submit_btn:
        try:
            submit_btn.click(timeout=5000)
        except Exception:
            submit_btn.evaluate("e => e.click()")
    else:
        page.keyboard.press("Enter")

    page.wait_for_timeout(8000)

    logger.log(
        "navigate",
        "post-verification",
        reasoning="OTP submitted, proceeding to application form",
        confidence="high",
    )
    return True


# Imported after _handle_identity_verification is defined so the verification,
# recovery, navigation import cycle resolves no matter which module loads first.
from q2apply.recovery import _save_debug_snapshot  # noqa: E402


def _await_verification_result(page, logger: DecisionLogger) -> str:
    """Poll for verification result: submitted, continue, or failed."""
    for wait_round in range(4):
        page.wait_for_timeout(3000)

        if _detect_submission_success(page):
            logger.log(
                "submit",
                "verification",
                reasoning="Submitted after verification code",
                confidence="high",
            )
            return "submitted"

        # If verification page is gone, the code worked -- continue the form
        if not _detect_email_verification(page):
            logger.log(
                "navigate",
                "post-verification",
                reasoning="Verification page cleared, code accepted",
                confidence="high",
            )
            return "continue"

        # Check for explicit error messages on the verification page
        try:
            error_text = page.evaluate(
                "() => (document.body ? document.body.innerText : '').toLowerCase()"
            )
            if any(
                phrase in error_text
                for phrase in ("invalid code", "expired", "incorrect code", "try again")
            ):
                logger.log(
                    "abort",
                    "verification",
                    reasoning="Verification code was invalid or expired",
                    confidence="high",
                )
                return "failed: verification code invalid/expired"
        except Exception:
            pass

        if wait_round < 3:
            log.debug("   Verification page still showing, waiting... (attempt %d)", wait_round + 2)

    _save_debug_snapshot(page, "verification", "code_rejected")
    logger.log(
        "abort",
        "verification",
        reasoning="Verification code may have been rejected (page unchanged after 12s)",
        confidence="uncertain",
    )
    return "failed: verification code rejected"


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
        profile.email, profile.gmail_app_password, max_wait=120
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

    return _await_verification_result(page, logger)
