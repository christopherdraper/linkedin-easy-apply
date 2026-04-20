"""Workday ATS handler.

Handles Workday-specific quirks:
- Cookie banner dismissal (OneTrust overlays)
- "Autofill with Resume" / "Apply Manually" popups
- Login wall resolution with Workday-specific account creation
- React SPA-compatible consent checkbox and submit button handling
"""

import logging

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register

log = logging.getLogger("job_apply")


class WorkdayHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Workday"

    def pre_flight(self, page, ctx):
        self._dismiss_cookie_banner(page)
        return None

    def on_step_start(self, page, ctx):
        # "Start Your Application" popup -- "Autofill with Resume"
        try:
            autofill = page.query_selector("a[data-automation-id='autofillWithResume']")
            if autofill and autofill.is_visible():
                from job_search_apply import _safe_click

                _safe_click(autofill, page)
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:  # noqa: BLE001, S110
                    pass
                ctx["skip_step"] = True
                return None
        except Exception:  # noqa: BLE001, S110
            pass

        # Fallback: "Apply Manually"
        try:
            manual = page.query_selector("a[data-automation-id='applyManually']")
            if manual and manual.is_visible():
                from job_search_apply import _safe_click

                _safe_click(manual, page)
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:  # noqa: BLE001, S110
                    pass
                ctx["skip_step"] = True
                return None
        except Exception:  # noqa: BLE001, S110
            pass

        # Re-dismiss cookie banner (can reappear after navigation)
        self._dismiss_cookie_banner(page)
        return None

    def resolve_login_wall(self, page, ctx: dict) -> bool:
        """Handle Workday login/registration pages.

        Workday keeps password fields in the DOM even on application forms,
        causing false login wall detections. When on a genuine login page,
        attempts Workday-specific account creation with React-compatible selectors.
        """
        # Check if this is genuinely a login/registration page
        # (vs a form page with password fields still in the DOM)
        try:
            body_text = page.evaluate("document.body?.innerText?.slice(0, 3000) || ''").lower()
        except Exception:  # noqa: BLE001
            return True  # can't read page, assume not a real login wall

        has_create_option = "create account" in body_text or "sign up" in body_text
        has_sign_in_prompt = "sign in" in body_text and (
            "already have" in body_text or "existing" in body_text
        )

        if not has_create_option and not has_sign_in_prompt:
            # Not a real login page -- just Workday keeping auth fields in DOM
            return True

        profile = ctx.get("profile")
        if not profile:
            return False

        # Try stored credentials first
        from job_search_apply import _attempt_ats_login, _get_domain

        domain = _get_domain(page.url)
        if _attempt_ats_login(page, domain):
            log.info("   Workday: logged in with stored credentials")
            return True

        # Try Workday-specific account creation
        if getattr(profile, "auto_create_accounts", False):
            return self._create_workday_account(page, profile)

        return False

    def _create_workday_account(self, page, profile) -> bool:
        """Create a Workday account using React-SPA-compatible selectors."""
        from job_search_apply import (
            _attempt_ats_login,
            _fill_registration_form,
            _generate_ats_password,
            _get_domain,
            _handle_registration_verification,
            _safe_click,
            _save_ats_account,
        )

        # Step 1: Navigate to registration form
        try:
            create_link = page.query_selector(
                "a:has-text('Create Account'), a:has-text('Create an Account'), "
                "button:has-text('Create Account'), a:has-text('Sign Up'), "
                "a:has-text('New User'), a:has-text('Don\\'t have an account')"
            )
            if create_link and create_link.is_visible():
                log.info("   Workday: clicking Create Account link")
                _safe_click(create_link, page)
                page.wait_for_timeout(2000)
        except Exception:  # noqa: BLE001, S110
            pass

        # Dismiss cookie banner that may have appeared
        self._dismiss_cookie_banner(page)

        # Step 2: Fill basic registration fields (generic function handles
        # email, password, name -- these work fine on Workday)
        password = _generate_ats_password()
        fields_filled = _fill_registration_form(page, profile, password)

        if fields_filled < 2:
            log.info("   Workday: too few registration fields (%d)", fields_filled)
            return False

        log.info("   Workday: filled %d registration fields", fields_filled)

        # Step 3: Handle consent checkbox (React component, not native input)
        self._check_consent_checkbox(page)

        # Step 4: Click Create Account submit button (React-compatible)
        if not self._click_submit_button(page):
            log.info("   Workday: could not click Create Account button")
            return False

        page.wait_for_timeout(4000)

        # Step 5: Handle email verification if required
        if not _handle_registration_verification(page, profile):
            return False

        # Step 6: Check outcome
        page.wait_for_timeout(2000)
        body = page.evaluate("document.body?.innerText?.toLowerCase()?.slice(0, 2000) || ''")
        still_has_password = bool(page.query_selector("input[type='password']:visible"))

        if still_has_password:
            # Check for "already exists" -- try login instead
            if any(s in body for s in ("already exists", "already registered", "email is already")):
                log.info("   Workday: account already exists, trying login")
                domain = _get_domain(page.url)
                return _attempt_ats_login(page, domain)
            if not any(s in body for s in ("account created", "welcome", "success")):
                log.info("   Workday: registration may not have succeeded")
                return False

        domain = _get_domain(page.url)
        _save_ats_account(domain, profile.email, password)
        log.info("   Workday: account created on %s", domain)
        return True

    @staticmethod
    def _check_consent_checkbox(page) -> None:
        """Check Workday consent checkboxes (React components, not native inputs).

        Workday renders checkboxes as custom React components. Native selectors
        like input[type='checkbox'] don't work. Uses JS to find and click
        elements containing consent text.
        """
        try:
            # Try native checkbox first (some Workday instances use them)
            native_cb = page.query_selector(
                "input[type='checkbox'][name*='agree' i], "
                "input[type='checkbox'][name*='terms' i], "
                "input[type='checkbox'][name*='understand' i], "
                "input[type='checkbox'][id*='agree' i]"
            )
            if native_cb and native_cb.is_visible() and not native_cb.is_checked():
                native_cb.check()
                log.info("   Workday: checked consent (native checkbox)")
                return

            # React checkbox: find clickable element near consent text
            checked = page.evaluate("""() => {
                // Look for React checkbox components near consent text
                const allEls = document.querySelectorAll('*');
                for (const el of allEls) {
                    const text = (el.textContent || '').trim();
                    if (text.length > 5 && text.length < 500
                        && (/i understand|i agree|i acknowledge|privacy policy/i.test(text))) {
                        // Find checkbox-role element inside or nearby
                        const cb = el.querySelector(
                            '[role="checkbox"], [data-automation-id*="check"], '
                            + '[class*="checkbox"], [class*="Checkbox"]'
                        );
                        if (cb) {
                            cb.click();
                            return 'role-checkbox';
                        }
                        // Click the label/container itself
                        if (el.tagName === 'LABEL' || el.closest('label')) {
                            (el.closest('label') || el).click();
                            return 'label-click';
                        }
                        // Last resort: click the element itself
                        if (el.children.length <= 5) {
                            el.click();
                            return 'direct-click';
                        }
                    }
                }
                return null;
            }""")
            if checked:
                page.wait_for_timeout(500)
                log.info("   Workday: checked consent (%s)", checked)
        except Exception as e:  # noqa: BLE001
            log.debug("Workday consent checkbox: %s", e)

    @staticmethod
    def _click_submit_button(page) -> bool:
        """Click Workday Create Account button using React-compatible approach.

        Workday's React SPA renders buttons that don't respond to CSS
        :has-text() selectors. Uses data-automation-id first, then JS
        text matching with direct click.
        """
        try:
            # Strategy 1: Workday data-automation-id selectors
            btn = page.query_selector(
                "[data-automation-id='createAccountSubmitButton'], "
                "[data-automation-id='click_filter'][aria-label='Create Account']"
            )
            if btn and btn.is_visible():
                btn.click()
                log.info("   Workday: clicked submit (data-automation-id)")
                return True

            # Strategy 2: JS text match + click (bypasses React SPA rendering)
            clicked = page.evaluate("""() => {
                const buttons = document.querySelectorAll(
                    'button, [role="button"], div[tabindex="0"], a[role="button"]'
                );
                for (const btn of buttons) {
                    const text = (btn.textContent || '').trim();
                    if (/^Create Account$/i.test(text) || /^Sign Up$/i.test(text)) {
                        btn.click();
                        return text;
                    }
                }
                return null;
            }""")
            if clicked:
                log.info("   Workday: clicked submit (JS text match: %s)", clicked)
                return True

            # Strategy 3: Generic submit button
            submit = page.query_selector("button[type='submit'], input[type='submit']")
            if submit and submit.is_visible():
                submit.click()
                log.info("   Workday: clicked submit (generic submit button)")
                return True

            return False
        except Exception as e:  # noqa: BLE001
            log.debug("Workday submit button click failed: %s", e)
            return False

    def q2_pre_flight(self, page, ctx):
        self._dismiss_cookie_banner(page)
        return None

    def q2_resolve_login_wall(self, page, ctx):
        return True

    @staticmethod
    def _dismiss_cookie_banner(page):
        try:
            btn = page.query_selector(
                "#onetrust-accept-btn-handler, "
                "[data-testid='cookie-accept'], "
                "button:has-text('Accept All'):visible, "
                "button:has-text('Accept all'):visible, "
                "button:has-text('Accept Cookies'):visible"
            )
            if btn and btn.is_visible():
                from job_search_apply import _safe_click

                _safe_click(btn, page)
                page.wait_for_timeout(1000)
        except Exception:  # noqa: BLE001, S110
            pass


register("Workday", WorkdayHandler)
