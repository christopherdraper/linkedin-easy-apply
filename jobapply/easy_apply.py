"""LinkedIn Easy Apply: the multi-step modal form state machine and its
submit entry point."""

import logging
import random
import time
from pathlib import Path
from typing import Dict, Optional

from jobapply import stats
from jobapply.browser import _playwright_context, _save_session, _stealth_playwright
from jobapply.forms import (
    _answer_screening_questions,
    _dismiss_all_typeaheads,
    _dump_form_debug,
    _fill_empty_required_fields,
    _get_validation_errors,
    _safe_click,
)
from jobapply.profile import ApplicantProfile
from jobapply.safety import ApplicationAbortError

log = logging.getLogger(__name__)


def _dismiss_save_dialog(page) -> None:
    """Dismiss the 'Save this application?' dialog if it appeared."""
    try:
        discard_btn = page.query_selector(
            "button:has-text('Discard'), button[data-test-dialog-secondary-btn]"
        )
        if discard_btn and discard_btn.is_visible():
            discard_btn.click()
            page.wait_for_timeout(1000)
            log.debug("Dismissed 'Save this application?' dialog")
    except Exception as exc:
        log.debug("Save dialog dismiss failed (non-critical): %s", exc)


def _get_modal_text(page) -> str:
    """Get the text content of the Easy Apply modal for stall detection."""
    modal = page.query_selector(".artdeco-modal, .jobs-easy-apply-modal, [role='dialog']")
    if modal:
        try:
            return modal.inner_text()[:800]
        except Exception:  # noqa: S110
            log.debug("Failed to read modal text for stall detection")
    return ""


def _navigate_form(page, profile, owns_browser, context, job_id: str = "") -> str:  # noqa: C901
    """Navigate through multi-step Easy Apply form. Returns status string."""
    max_steps = 15
    stalled = 0
    for _step in range(max_steps):
        page.wait_for_timeout(random.randint(800, 2200))

        # Dismiss "Save this application?" dialog if it appeared
        _dismiss_save_dialog(page)

        # Check if modal is still open
        modal = page.query_selector(".artdeco-modal, .jobs-easy-apply-modal, [role='dialog']")
        if not modal:
            _dump_form_debug(page, job_id, "Application modal closed unexpectedly")
            return "failed: lost track of form steps"

        # Fill screening questions on EVERY page (new fields appear after each Next)
        _answer_screening_questions(page, profile)

        submit_btn = page.query_selector(
            "button[aria-label='Submit application'], button[aria-label*='Submit']"
        )
        review_btn = page.query_selector(
            "button[aria-label='Review your application'], button[aria-label*='Review']"
        )
        next_btn = page.query_selector(
            "button[aria-label='Continue to next step'], button[aria-label*='Continue']"
        )

        if submit_btn:
            _dismiss_all_typeaheads(page)
            _safe_click(submit_btn, page)
            # Wait for confirmation with retries — LinkedIn can be slow to render
            success = None
            confirmation_sel = (
                "[aria-label='Your application was sent'], "
                ".artdeco-modal__header:has-text('Application submitted'), "
                "h2:has-text('application was sent'), "
                ".artdeco-inline-feedback--success, "
                "[data-test-modal-id='post-apply-modal']"
            )
            try:
                success = page.wait_for_selector(confirmation_sel, timeout=8000)
            except Exception:
                # Final fallback: check if form validation errors appeared (means it didn't submit)
                validation_err = page.query_selector(
                    ".artdeco-inline-feedback--error, "
                    "[data-test-form-element-error], "
                    ".fb-dash-form-element__error-text"
                )
                if validation_err:
                    err_text = validation_err.inner_text()[:200]
                    if owns_browser:
                        _save_session(context)
                    _dump_form_debug(
                        page, job_id, f"Submit clicked but validation errors: {err_text}"
                    )
                    return f"failed: form validation errors after submit: {err_text}"
                # No confirmation AND no errors — check once more
                success = page.query_selector(confirmation_sel)
            if owns_browser:
                _save_session(context)
            return "submitted" if success else "submitted (unconfirmed — check LinkedIn)"
        elif review_btn:
            _dismiss_all_typeaheads(page)
            cur_text = _get_modal_text(page)
            _safe_click(review_btn, page)
            page.wait_for_timeout(1500)
            _dismiss_save_dialog(page)
            new_text = _get_modal_text(page)
            if new_text == cur_text:
                # Stalled — check for validation errors and try to fix them
                errors = _get_validation_errors(page)
                filled = _fill_empty_required_fields(page, profile)
                if filled > 0:
                    log.debug("Review stalled: filled %d fields, retrying", filled)
                    stalled = 0  # reset — we made progress
                else:
                    stalled += 1
                    err_hint = f" errors={errors}" if errors else ""
                    log.debug("Review click didn't change modal (stalled=%d)%s", stalled, err_hint)
                    if stalled >= 3:
                        reason = f"Review not advancing (step {_step + 1}/{max_steps})"
                        if errors:
                            reason += f" (validation: {'; '.join(errors)[:200]})"
                        _dump_form_debug(page, job_id, reason)
                        return f"failed: form stuck — {reason}"
            else:
                stalled = 0
        elif next_btn:
            _dismiss_all_typeaheads(page)
            cur_text = _get_modal_text(page)
            _safe_click(next_btn, page)
            page.wait_for_timeout(1500)
            _dismiss_save_dialog(page)
            new_text = _get_modal_text(page)
            if new_text == cur_text:
                # Stalled — check for validation errors and try to fix them
                errors = _get_validation_errors(page)
                filled = _fill_empty_required_fields(page, profile)
                if filled > 0:
                    log.debug("Next stalled: filled %d fields, retrying", filled)
                    stalled = 0  # reset — we made progress
                else:
                    stalled += 1
                    err_hint = f" errors={errors}" if errors else ""
                    log.debug("Next click didn't change modal (stalled=%d)%s", stalled, err_hint)
                    if stalled >= 3:
                        reason = f"Next not advancing (step {_step + 1}/{max_steps})"
                        if errors:
                            reason += f" (validation: {'; '.join(errors)[:200]})"
                        _dump_form_debug(page, job_id, reason)
                        return f"failed: form stuck — {reason}"
            else:
                stalled = 0
        else:
            stalled += 1
            if stalled >= 3:
                _dump_form_debug(page, job_id, "No Next/Submit/Review buttons found")
                return "failed: lost track of form steps"
            page.wait_for_timeout(1500)

    _dump_form_debug(page, job_id, "Exceeded max form steps")
    return "failed: exceeded max form steps (15)"


def submit_easy_apply(job: Dict, profile: ApplicantProfile, proxy: Optional[str] = None) -> str:
    """
    Submit a LinkedIn Easy Apply application.
    Returns 'submitted' on success, 'failed: <reason>' on failure.
    """
    stats._apply_start_time = time.time()
    stats._field_fills.clear()
    stats._ai_answer_failures.clear()
    stats._ai_tokens_in = 0
    stats._ai_tokens_out = 0
    stats._final_ats_url = ""
    try:
        import playwright  # noqa: F401
    except ImportError:
        raise RuntimeError("Playwright not installed") from None

    with _stealth_playwright() as p:
        browser, context, page, owns_browser = _playwright_context(p, proxy)
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)

            easy_apply_sel = "[aria-label*='Easy Apply'], button:has-text('Easy Apply'), a:has-text('Easy Apply')"
            easy_apply_btn = None
            for _wait in range(4):
                easy_apply_btn = page.query_selector(easy_apply_sel)
                if easy_apply_btn:
                    break
                page.wait_for_timeout(2000)
            if not easy_apply_btn:
                return "failed: Easy Apply button not found — job may no longer be active"
            _safe_click(easy_apply_btn, page)
            page.wait_for_timeout(1500)

            phone_field = page.query_selector("input[name='phoneNumber']")
            if phone_field:
                phone_field.fill(profile.phone)

            resume_path = str(Path(profile.resume_path).expanduser())
            resume_input = page.query_selector(
                "input[type='file'][name*='resume'], input[type='file'][accept*='pdf']"
            )
            if resume_input:
                resume_input.set_input_files(resume_path)

            _answer_screening_questions(page, profile)
            return _navigate_form(page, profile, owns_browser, context, job_id=job.get("id", ""))

        except ApplicationAbortError as e:
            log.warning(f"   🛡️  Application aborted: {e}")
            return f"aborted: {e}"
        except Exception as e:
            return f"failed: {e}"
        finally:
            page.close()
            if owns_browser:
                browser.close()
