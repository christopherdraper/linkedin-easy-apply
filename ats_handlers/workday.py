"""Workday ATS handler.

Handles Workday-specific quirks:
- Cookie banner dismissal (OneTrust overlays)
- "Autofill with Resume" / "Apply Manually" popups
- Login wall resolution with Workday-specific account creation
- React SPA-compatible consent checkbox and submit button handling
"""

import logging
import os
import re

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register

log = logging.getLogger("job_apply")

WD_REVIEW_PARKED_STATUS = "review_parked: manual submit required"
WD_SUBMIT_UNCONFIRMED_STATUS = (
    "submitted: unconfirmed (Submit clicked, no confirmation seen; check Candidate Home)"
)

# Evidence that Workday accepted the application. Deliberately excludes the
# generic "thank you for your interest" that Candidate Home shows regardless.
_WD_CONFIRMED_RE = re.compile(
    r"application (has been |was )?(successfully )?submitted|congratulations|"
    r"thank(s| you) for (applying|your application)|"
    r"(we('ve| have)|has been) received your application",
    re.I,
)
_WD_PAGE_STATE_JS = r"""() => ({
  text: (document.body.innerText || '').slice(0, 6000),
  errors: [...document.querySelectorAll(
      "[data-automation-id='errorMessage'], [data-automation-id='errorBanner'], "
      + "[data-automation-id='errorWidgetBar']")]
      .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 3),
})"""


def _park_workday_at_review() -> bool:
    """Kill switch: JOBAPPLY_WORKDAY_PARK=1 restores park-at-Review (no submit)."""
    return os.environ.get("JOBAPPLY_WORKDAY_PARK") == "1"


def _click_workday_button(element, page) -> None:
    from job_search_apply import _safe_click

    _safe_click(element, page)


_WD_MAX_VISION_PASSES = 3  # per-application cap; each pass drives the whole form (120 actions)
_WD_MAX_ERROR_RELOADS = 2  # per-application cap on "Something went wrong" reloads


class WorkdayHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Workday"

    def pre_flight(self, page, ctx):
        self._dismiss_cookie_banner(page)
        return None

    def on_step_start(self, page, ctx):
        # Close blocking Workday confirmation modals BEFORE anything else --
        # if a 'Change Email' / similar dialog opens mid-form, the form-step
        # loop fills 0 fields per iteration until it hits max steps (20).
        self._close_blocking_modal(page)

        # "Start Your Application" popup -- "Autofill with Resume" / "Apply
        # Manually". Only ever fires once per application: after it clicks
        # successfully once, ctx["_wd_autofilled"] latches it off so it never
        # re-fires on a later page that happens to reuse similar markup.
        if not ctx.get("_wd_autofilled"):
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
                    ctx["_wd_autofilled"] = True
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
                    ctx["_wd_autofilled"] = True
                    ctx["skip_step"] = True
                    return None
            except Exception:  # noqa: BLE001, S110
                pass

        # Re-dismiss cookie banner (can reappear after navigation)
        self._dismiss_cookie_banner(page)

        # Workday's SPA sometimes drops a step and renders "Something went
        # wrong / refresh the page". Do what it asks before anything else reads
        # the page, otherwise the step is scored as "no nav button" and lost.
        if self._recover_from_error_page(page, ctx):
            ctx["skip_step"] = True
            return None

        if self._is_review_page(page) or self._at_terminal_submit(page):
            # Owner switched Workday from park-at-Review to submit on 2026-09-24.
            if _park_workday_at_review():
                log.info("   Workday: Review page reached -- parking (JOBAPPLY_WORKDAY_PARK=1)")
                return WD_REVIEW_PARKED_STATUS
            log.info("   Workday: Review page reached -- submitting")
            return self._submit_review(page)

        # Workday form pages: the generic deterministic dropdown handling corrupts
        # Workday's searchable dropdowns (it reads merged option lists and fills
        # garbage, so the page never validates or advances). Drive each form page
        # with vision instead and SKIP the deterministic fill entirely. Bounded per
        # application by _WD_MAX_VISION_PASSES to cap cost.
        profile = ctx.get("profile")
        if profile is not None and self._is_form_page(page):
            passes = ctx.get("_wd_vpasses", 0)
            if passes < _WD_MAX_VISION_PASSES:
                ctx["_wd_vpasses"] = passes + 1
                from ats_handlers import _workday_vision

                log.info("   Workday: vision completing form page (pass %d)", passes + 1)
                try:
                    _workday_vision.vision_complete_page(page, profile)
                except Exception as e:  # noqa: BLE001
                    log.debug("Workday page vision failed: %s", str(e)[:100])
                ctx["skip_step"] = True  # bypass the corrupting deterministic fill

        return None

    @staticmethod
    def _submit_review(page) -> str:
        """Click Submit on the Review page and report only what Workday confirms.

        Returns "submitted" on an explicit confirmation, "failed: ..." when the
        page shows errors (before or after the click), and an unconfirmed
        "submitted: ..." status when Submit was clicked but no confirmation
        appeared: never a plain success without evidence, and never a
        "failed" that could get the same requisition submitted twice.
        """

        def state():
            st = page.evaluate(_WD_PAGE_STATE_JS)
            return st if isinstance(st, dict) else {}

        try:
            before = state()
        except Exception as e:  # noqa: BLE001  (nothing clicked yet: safe to fail)
            return f"failed: could not read Workday Review page: {str(e)[:120]}"
        if before.get("errors"):
            return f"failed: Workday Review page reports errors: {before['errors'][0][:150]}"
        already_confirmed = bool(_WD_CONFIRMED_RE.search(before.get("text", "")))

        submit = None
        for el in page.query_selector_all(
            "[data-automation-id='pageFooterNextButton'], button, [role='button']"
        ):
            try:
                if el.is_visible() and re.match(r"^\s*submit\b", el.inner_text() or "", re.I):
                    submit = el
                    break
            except Exception:  # noqa: BLE001, S112
                continue
        if submit is None:
            return "failed: Workday Submit button not found on Review page"

        _click_workday_button(submit, page)
        # From here on Submit may have gone through: an error must NOT become a
        # "failed" status, which could get the same requisition submitted again.
        for _ in range(10):
            try:
                page.wait_for_timeout(2000)
                after = state()
            except Exception as e:  # noqa: BLE001
                log.warning("   ⚠️ Workday: lost the page after Submit (%s)", str(e)[:80])
                return WD_SUBMIT_UNCONFIRMED_STATUS
            if after.get("errors"):
                return f"failed: Workday rejected the submission: {after['errors'][0][:150]}"
            if not already_confirmed and _WD_CONFIRMED_RE.search(after.get("text", "")):
                log.info("   ✅ Workday: submission confirmed")
                return "submitted"
        log.warning("   ⚠️ Workday: Submit clicked but no confirmation seen")
        return WD_SUBMIT_UNCONFIRMED_STATUS

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

    @staticmethod
    def _is_error_page(page) -> bool:
        """True on Workday's "Something went wrong" panel.

        Workday's SPA intermittently drops a step (seen twice on the My
        Information step of a Rolls-Royce req) and renders an error panel whose
        own instruction is to refresh. It carries no form fields and no footer
        button, so the generic loop reads it as "no nav button", burns its three
        retries and fails the application.
        """
        try:
            text = page.evaluate("document.body?.innerText?.toLowerCase() || ''")
            return "something went wrong" in text and "refresh the page" in text
        except Exception:  # noqa: BLE001
            return False

    def _recover_from_error_page(self, page, ctx) -> bool:
        """Reload past a Workday error panel. True if a reload was performed.

        Bounded by _WD_MAX_ERROR_RELOADS so a genuinely broken application
        still fails instead of reloading forever.
        """
        if not self._is_error_page(page):
            return False

        reloads = ctx.get("_wd_error_reloads", 0)
        if reloads >= _WD_MAX_ERROR_RELOADS:
            log.warning("   Workday: error page persists after %d reloads", reloads)
            return False

        ctx["_wd_error_reloads"] = reloads + 1
        log.info("   Workday: 'Something went wrong' -- reloading (%d)", reloads + 1)
        try:
            page.reload(wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
        except Exception as exc:  # noqa: BLE001
            log.debug("Workday: reload failed: %s", str(exc)[:100])
            return False

        self._dismiss_cookie_banner(page)
        return True

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
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:  # noqa: BLE001, S110
            pass

        # Step 5: Handle email verification if required
        if not _handle_registration_verification(page, profile):
            return False

        # Step 6: Check outcome
        page.wait_for_timeout(3000)
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
        """Check Workday consent checkboxes.

        Workday renders checkboxes as either native inputs (rare) or React
        components. Strategies, in order:
        1. Workday data-automation-id for consent checkbox
        2. Any visible unchecked native checkbox on the page
        3. JS: walk DOM from consent text to find nearby checkbox element
        """
        try:
            # Strategy 1: Workday data-automation-id
            wd_cb = page.query_selector(
                "[data-automation-id='createAccountCheckbox'], "
                "[data-automation-id='termsCheckbox'], "
                "[data-automation-id='privacyCheckbox']"
            )
            if wd_cb and wd_cb.is_visible():
                wd_cb.click()
                page.wait_for_timeout(500)
                log.info("   Workday: checked consent (data-automation-id)")
                return

            # Strategy 2: Any visible unchecked native checkbox
            # Registration pages typically have exactly one checkbox (consent)
            all_cbs = page.query_selector_all("input[type='checkbox']")
            for cb in all_cbs:
                try:
                    if cb.is_visible() and not cb.is_checked():
                        cb.check()
                        page.wait_for_timeout(500)
                        log.info("   Workday: checked consent (native checkbox)")
                        return
                except Exception:  # noqa: BLE001, S110
                    pass

            # Strategy 3: JS -- find consent text, then walk up to find
            # the checkbox element (child, sibling, or in parent container)
            checked = page.evaluate("""() => {
                const consentRe = /i understand|i agree|i acknowledge|checking this box/i;
                const cbSel = '[role="checkbox"], [data-automation-id*="check"], '
                    + '[data-automation-id*="Check"], input[type="checkbox"], '
                    + '[class*="checkbox" i]';

                const allEls = document.querySelectorAll('*');
                for (const el of allEls) {
                    const text = (el.textContent || '').trim();
                    if (text.length < 10 || text.length > 500 || !consentRe.test(text))
                        continue;

                    // Check children first
                    let cb = el.querySelector(cbSel);
                    if (cb) { cb.click(); return 'child'; }

                    // Check siblings
                    if (el.parentElement) {
                        cb = el.parentElement.querySelector(cbSel);
                        if (cb) { cb.click(); return 'sibling'; }
                    }

                    // Walk up 2 more levels
                    let parent = el.parentElement?.parentElement;
                    for (let i = 0; i < 2 && parent; i++, parent = parent.parentElement) {
                        cb = parent.querySelector(cbSel);
                        if (cb) { cb.click(); return 'ancestor-' + (i + 2); }
                    }
                }
                return null;
            }""")
            if checked:
                page.wait_for_timeout(500)
                log.info("   Workday: checked consent (JS: %s)", checked)
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
    def _close_blocking_modal(page):
        """Close Workday confirmation/change-email modals that block form
        progression.

        Strayer (and other Workday tenants) open a 'Change Email' modal mid-form
        when the bot reuses a stored account email. The modal sits over the
        application form and the form-step loop spins for 20 iterations because
        it can't see new fields under the modal. The modal has a heading id
        like ``changeEmailModal`` and a close button with ``aria-label="close"``.
        """
        try:
            closed = page.evaluate("""() => {
                // Find any Workday modal heading we want to dismiss
                const modalHeadingIds = [
                    'changeEmailModal',
                    'changeEmailHeader',
                ];
                for (const hid of modalHeadingIds) {
                    const h = document.getElementById(hid);
                    if (!h) continue;
                    // Walk up to find the modal container
                    let container = h;
                    for (let i = 0; i < 4 && container; i++) {
                        const closeBtn = container.querySelector('button[aria-label="close"], button[aria-label="Close"]');
                        if (closeBtn) {
                            closeBtn.click();
                            return hid;
                        }
                        container = container.parentElement;
                    }
                }
                return null;
            }""")
            if closed:
                page.wait_for_timeout(800)
                log.info("   Workday: closed blocking modal (%s)", closed)
        except Exception as e:  # noqa: BLE001
            log.debug("Workday close modal failed: %s", e)

    @staticmethod
    def _is_review_page(page) -> bool:
        """True only on the final Workday Review page (heading 'Review' AND a
        Submit button AND a review summary present) -- deliberately strict so we
        never stop early on an intermediate page that merely mentions 'review'.
        """
        try:
            return bool(
                page.evaluate(r"""() => {
              const heads = [...document.querySelectorAll('h1,h2,h3')]
                  .map(e => (e.innerText || '').trim().toLowerCase());
              const hasReviewHeading = heads.some(t => t === 'review' || t.startsWith('review'));
              const nav = [...document.querySelectorAll(
                  "[data-automation-id='pageFooterNextButton'], button, [role='button']")];
              const hasSubmit = nav.some(b => /^\s*submit\b/i.test(b.innerText || ''));
              const hasSummary = !!document.querySelector(
                  "[data-automation-id='summaryItem'], [data-automation-id='reviewPanel'], "
                  + "[data-automation-id='reviewSection']");
              return hasReviewHeading && hasSubmit && hasSummary;
            }""")
            )
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _at_terminal_submit(page) -> bool:
        """Selector-independent backstop for the Review-stop gate.

        `_is_review_page` depends on an unvalidated data-automation-id guess
        (hasSummary); if that guess is wrong on some Workday tenant, the gate
        fails OPEN and the shared loop can click Submit. This check does not
        depend on that selector: it returns True iff the only visible, enabled
        way forward on the page is a Submit control -- i.e. there is a visible
        enabled control whose text matches Submit and NO visible enabled
        forward control (Next / Continue / Save and continue). This only ADDS
        parking on top of `_is_review_page`; it never removes it, and it fails
        CLOSED (returns False) on any evaluate error so a detector bug cannot
        itself trigger parking.
        """
        try:
            return bool(
                page.evaluate(r"""() => {
              const isVisible = (el) => {
                  const r = el.getBoundingClientRect();
                  if (r.width < 1 || r.height < 1) return false;
                  const style = window.getComputedStyle(el);
                  return style.visibility !== 'hidden' && style.display !== 'none';
              };
              const isEnabled = (el) => {
                  if (el.disabled) return false;
                  if ((el.getAttribute('aria-disabled') || '').toLowerCase() === 'true')
                      return false;
                  return true;
              };
              const candidates = [...document.querySelectorAll(
                  'button, [role="button"], [data-automation-id="pageFooterNextButton"]')]
                  .filter(el => isVisible(el) && isEnabled(el));
              const textOf = (el) => (el.innerText || el.textContent || '');
              const hasSubmit = candidates.some(el => /\bsubmit\b/i.test(textOf(el)));
              const hasForward = candidates.some(
                  el => /next|continue|save and continue/i.test(textOf(el)));
              return hasSubmit && !hasForward;
            }""")
            )
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _is_form_page(page) -> bool:
        """True on a fillable Workday application form page: the footer
        Next/Save-and-Continue control is present AND there are form fields. Keeps
        the vision pass from firing on login/landing/confirmation states."""
        try:
            return bool(
                page.evaluate("""() => {
                  const nav = document.querySelector("[data-automation-id='pageFooterNextButton']");
                  const fields = document.querySelectorAll(
                    "[data-automation-id^='formField-'], input, textarea, [aria-haspopup='listbox']");
                  return !!nav && fields.length > 0;
                }""")
            )
        except Exception:  # noqa: BLE001
            return False


register("Workday", WorkdayHandler)
