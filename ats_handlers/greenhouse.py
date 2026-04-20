"""Greenhouse ATS handler.

Greenhouse quirks handled here:
- Email verification codes sent after form submission or mid-form
- Security code input fields (various selector strategies)
- Post-verification page transition polling
"""

import logging
from typing import Optional

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register

log = logging.getLogger("job_apply")


class GreenhouseHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Greenhouse"

    def handle_verification_code(self, page, ctx: dict) -> Optional[str]:  # noqa: C901
        """Handle Greenhouse email verification code flow.

        Fetches code via Gmail IMAP, enters it, polls for page transition.
        Returns 'submitted', 'continue', or 'failed: ...' status.
        Returns None if no gmail_app_password (falls through to generic handling).
        """
        profile = ctx.get("profile")
        if not profile or not profile.gmail_app_password:
            return None

        # Lazy imports to avoid circular dependency
        from job_search_apply import (
            _detect_success_or_confirmation,
            _extract_page_snapshot,
            _fetch_verification_code_from_gmail,
            _find_navigation_button,
            _get_field_label,
            _safe_click,
        )

        code = _fetch_verification_code_from_gmail(
            profile.email, profile.gmail_app_password, max_wait=45
        )
        if not code:
            log.warning("   Greenhouse: could not retrieve verification code from email")
            return "failed: verification code not received from email"

        # Find the security code input -- try multiple strategies
        code_input = None

        # Strategy 1: Playwright label-based locator
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

        # Strategy 3: empty visible text input near "code" label
        if not code_input:
            for inp in page.query_selector_all("input[type='text'], input:not([type])"):
                try:
                    if inp.is_visible() and not inp.input_value():
                        label = _get_field_label(page, inp)
                        if label and "code" in label.lower():
                            code_input = inp
                            break
                except Exception:
                    continue

        if not code_input:
            log.warning("   Greenhouse: got code but couldn't find input field")
            return "failed: verification code input not found"

        # Fill code using click + clear + type (React controlled components
        # reject programmatic fill)
        code_input.click()
        code_input.evaluate("el => el.value = ''")
        code_input.type(code, delay=50)
        page.wait_for_timeout(500)
        log.info("   Greenhouse: filled verification code: %s", code)

        # Submit
        submit_btn = page.query_selector(
            "button[type='submit'], input[type='submit'], button:has-text('Submit')"
        )
        if submit_btn:
            _safe_click(submit_btn, page)
        else:
            _, btn_el = _find_navigation_button(page)
            if btn_el:
                _safe_click(btn_el, page)

        page.wait_for_timeout(5000)

        # Check for success
        post_snap = _extract_page_snapshot(page)
        if _detect_success_or_confirmation(page, post_snap):
            log.info("   Greenhouse: application submitted after verification code")
            return "submitted"

        log.warning("   Greenhouse: verification code may have been rejected")
        return "failed: verification code rejected"


register("Greenhouse", GreenhouseHandler)
