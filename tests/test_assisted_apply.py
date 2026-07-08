"""Tests for Q2 assisted apply module."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assisted_apply_mcp import (  # noqa: E402
    MAX_PAGE_ATTEMPTS,
    DecisionLogger,
    _detect_email_verification,
    _detect_rejection,
    _detect_submission_success,
    _dismiss_cookie_banner,
    _execute_action_plan,
    _fill_pageup_combobox,
    _find_code_input,
    _find_otp_button,
    _find_submit_button,
    _match_field_to_profile,
    _run_page_loop,
)
from job_search_apply import ApplicantProfile  # noqa: E402


def _make_profile(**overrides):
    defaults = dict(
        full_name="Matt Draper",
        email="matt@example.com",
        phone="317-555-0100",
        city="Indianapolis",
        state="IN",
        zip_code="46203",
        country="US",
        linkedin_url="https://linkedin.com/in/mattdraper",
        resume_path="/tmp/resume.pdf",
        current_title="Senior SRE",
        current_employer="Acme",
        years_experience=12,
        screening_answers={"salary": "150000", "kubernetes": "5", "aws": "5"},
    )
    defaults.update(overrides)
    return ApplicantProfile(**defaults)


# ---------------------------------------------------------------------------
# DecisionLogger
# ---------------------------------------------------------------------------


class TestDecisionLogger:
    def test_log_entries(self):
        logger = DecisionLogger()
        logger.log("fill_field", "First Name", "Matt", "Profile match", "high")
        logger.log("click", "Submit", reasoning="Submit form", confidence="medium")

        entries = logger.entries()
        assert len(entries) == 2
        assert entries[0]["step"] == 1
        assert entries[0]["action"] == "fill_field"
        assert entries[0]["target"] == "First Name"
        assert entries[0]["value"] == "Matt"
        assert entries[0]["confidence"] == "high"
        assert entries[1]["step"] == 2
        assert entries[1]["action"] == "click"

    def test_empty_logger(self):
        logger = DecisionLogger()
        assert logger.entries() == []

    def test_entries_returns_copy(self):
        logger = DecisionLogger()
        logger.log("click", "btn")
        e1 = logger.entries()
        e1.clear()
        assert len(logger.entries()) == 1


# ---------------------------------------------------------------------------
# Profile field matching
# ---------------------------------------------------------------------------


class TestMatchFieldToProfile:
    def test_first_name_from_full_name(self):
        profile = _make_profile()
        assert _match_field_to_profile("First Name", profile) == "Matt"

    def test_last_name_from_full_name(self):
        profile = _make_profile()
        assert _match_field_to_profile("Last Name", profile) == "Draper"

    def test_full_name(self):
        profile = _make_profile()
        assert _match_field_to_profile("Full Name", profile) == "Matt Draper"

    def test_email(self):
        profile = _make_profile()
        assert _match_field_to_profile("Email Address", profile) == "matt@example.com"

    def test_phone(self):
        profile = _make_profile()
        assert _match_field_to_profile("Phone Number", profile) == "317-555-0100"

    def test_city(self):
        profile = _make_profile()
        assert _match_field_to_profile("City", profile) == "Indianapolis"

    def test_zip_code(self):
        profile = _make_profile()
        assert _match_field_to_profile("Zip Code", profile) == "46203"

    def test_linkedin(self):
        profile = _make_profile()
        result = _match_field_to_profile("LinkedIn URL", profile)
        assert result == "https://linkedin.com/in/mattdraper"

    def test_screening_answer_match(self):
        profile = _make_profile()
        assert _match_field_to_profile("salary expectations", profile) == "150000"

    def test_screening_answer_kubernetes(self):
        profile = _make_profile()
        assert _match_field_to_profile("years of kubernetes experience", profile) == "5"

    def test_no_match(self):
        profile = _make_profile()
        assert _match_field_to_profile("favorite color", profile) is None

    def test_case_insensitive(self):
        profile = _make_profile()
        assert _match_field_to_profile("EMAIL", profile) == "matt@example.com"


# ---------------------------------------------------------------------------
# Submission detection
# ---------------------------------------------------------------------------


class TestDetectSubmissionSuccess:
    def test_detects_thank_you(self):
        page = MagicMock()
        page.evaluate.return_value = "Thank you for applying! We will review your application."
        assert _detect_submission_success(page) is True

    def test_detects_application_submitted(self):
        page = MagicMock()
        page.evaluate.return_value = "Your application submitted successfully"
        assert _detect_submission_success(page) is True

    def test_no_success_text(self):
        page = MagicMock()
        page.evaluate.return_value = "Please fill in all required fields"
        assert _detect_submission_success(page) is False

    def test_handles_exception(self):
        page = MagicMock()
        page.evaluate.side_effect = Exception("page closed")
        assert _detect_submission_success(page) is False


# ---------------------------------------------------------------------------
# Action execution
# ---------------------------------------------------------------------------


class TestExecuteActionPlan:
    def _make_action(self, **kwargs):
        defaults = {
            "action": "fill",
            "ref": "E1",
            "target": "First Name",
            "value": "Matt",
            "reasoning": "test",
            "confidence": "high",
        }
        defaults.update(kwargs)
        return defaults

    def test_fill_action(self):
        page = MagicMock()
        el = MagicMock()
        el.evaluate.return_value = "input"
        page.query_selector.return_value = el
        profile = _make_profile()
        logger = DecisionLogger()

        result = _execute_action_plan(
            self._make_action(), page, profile, "/tmp/resume.pdf", "SRE", "Co", logger
        )
        assert result is True
        assert logger.entries()[0]["action"] == "fill_field"

    def test_click_action(self):
        page = MagicMock()
        el = MagicMock()
        page.query_selector.return_value = el
        profile = _make_profile()
        logger = DecisionLogger()

        result = _execute_action_plan(
            self._make_action(action="click", target="Next button"),
            page,
            profile,
            "",
            "SRE",
            "Co",
            logger,
        )
        assert result is True
        assert logger.entries()[0]["action"] == "click"

    def test_skip_action(self):
        page = MagicMock()
        # _retry_skipped_with_ai queries the element; return None so it skips
        page.query_selector.return_value = None
        profile = _make_profile()
        logger = DecisionLogger()

        result = _execute_action_plan(
            self._make_action(action="skip", target="Unknown field"),
            page,
            profile,
            "",
            "SRE",
            "Co",
            logger,
        )
        assert result is False
        assert logger.entries()[0]["action"] == "skip"

    def test_upload_with_resume(self):
        page = MagicMock()
        el = MagicMock()
        page.query_selector.return_value = el
        profile = _make_profile()
        logger = DecisionLogger()

        with patch("assisted_apply_mcp.Path") as mock_path:
            mock_path.return_value.exists.return_value = True
            result = _execute_action_plan(
                self._make_action(action="upload", target="Resume"),
                page,
                profile,
                "/tmp/resume.pdf",
                "SRE",
                "Co",
                logger,
            )

        assert result is False  # upload doesn't count as "filled"
        assert logger.entries()[0]["action"] == "upload"

    def test_profile_override_on_fill(self):
        """Deterministic profile match should override AI-suggested value."""
        page = MagicMock()
        el = MagicMock()
        el.evaluate.return_value = "input"
        page.query_selector.return_value = el
        profile = _make_profile()
        logger = DecisionLogger()

        action = self._make_action(
            target="Email Address",
            value="wrong@email.com",
            confidence="medium",
        )
        _execute_action_plan(action, page, profile, "/tmp/resume.pdf", "SRE", "Co", logger)

        entry = logger.entries()[0]
        assert entry["value"] == "matt@example.com"
        assert entry["confidence"] == "high"


# ---------------------------------------------------------------------------
# Rejection detection
# ---------------------------------------------------------------------------


class TestDetectRejection:
    def _page_with(self, text):
        page = MagicMock()
        page.evaluate.return_value = text
        return page

    def test_spam_filter(self):
        page = self._page_with("Your application was flagged as possible spam by our system.")
        assert _detect_rejection(page) == "spam filter"

    def test_spam_filter_case_insensitive(self):
        page = self._page_with("FLAGGED AS SPAM")
        assert _detect_rejection(page) == "spam filter"

    def test_submission_blocked(self):
        page = self._page_with("We couldn't submit your application at this time.")
        assert _detect_rejection(page) == "submission blocked"

    def test_duplicate_application(self):
        page = self._page_with("It looks like you have already applied for this position.")
        assert _detect_rejection(page) == "duplicate application"

    def test_position_closed(self):
        page = self._page_with("This position has been filled.")
        assert _detect_rejection(page) == "position closed"

    def test_no_longer_accepting(self):
        page = self._page_with("We are no longer accepting applications for this role.")
        assert _detect_rejection(page) == "position closed"

    def test_clean_page_returns_none(self):
        page = self._page_with("Please complete the remaining fields to continue.")
        assert _detect_rejection(page) is None

    def test_exception_returns_none(self):
        page = MagicMock()
        page.evaluate.side_effect = Exception("page closed")
        assert _detect_rejection(page) is None


# ---------------------------------------------------------------------------
# Email verification detection
# ---------------------------------------------------------------------------


class TestDetectEmailVerification:
    def test_truthy_evaluate_result(self):
        page = MagicMock()
        page.evaluate.return_value = True
        assert _detect_email_verification(page) is True

    def test_falsy_evaluate_result(self):
        page = MagicMock()
        page.evaluate.return_value = False
        assert _detect_email_verification(page) is False

    def test_js_source_pins_phrase_list(self):
        """The evaluate script carries the detection phrases."""
        page = MagicMock()
        page.evaluate.return_value = False
        _detect_email_verification(page)
        js_source = page.evaluate.call_args[0][0]
        assert "verification code was sent" in js_source
        assert "security code" in js_source
        assert "verify your email" in js_source

    def test_exception_returns_false(self):
        page = MagicMock()
        page.evaluate.side_effect = Exception("page closed")
        assert _detect_email_verification(page) is False


# ---------------------------------------------------------------------------
# Submit button finder
# ---------------------------------------------------------------------------


def _make_button(text, visible=True):
    btn = MagicMock()
    btn.text_content.return_value = text
    btn.is_visible.return_value = visible
    return btn


class TestFindSubmitButton:
    def test_found_via_primary_selector(self):
        page = MagicMock()
        btn = _make_button("Submit Application")
        page.query_selector.return_value = btn

        assert _find_submit_button(page) is btn
        first_selector = page.query_selector.call_args_list[0][0][0]
        assert first_selector == 'button:has-text("Submit Application")'

    def test_found_via_type_submit_fallback(self):
        page = MagicMock()
        btn = _make_button("Go")

        def qs(sel):
            return btn if sel == 'button[type="submit"]' else None

        page.query_selector.side_effect = qs
        assert _find_submit_button(page) is btn

    def test_skips_save_and_exit(self):
        page = MagicMock()
        page.query_selector.return_value = _make_button("Save and exit")
        assert _find_submit_button(page) is None

    def test_skip_text_then_uses_later_selector(self):
        page = MagicMock()
        skip_btn = _make_button("Save draft")
        good_btn = _make_button("Save and continue")

        def qs(sel):
            if sel == 'button:has-text("Submit Application")':
                return skip_btn
            if sel == 'button:has-text("Save and continue")':
                return good_btn
            return None

        page.query_selector.side_effect = qs
        assert _find_submit_button(page) is good_btn

    def test_invisible_button_skipped(self):
        page = MagicMock()
        page.query_selector.return_value = _make_button("Submit", visible=False)
        assert _find_submit_button(page) is None

    def test_nothing_found_returns_none(self):
        page = MagicMock()
        page.query_selector.return_value = None
        assert _find_submit_button(page) is None


# ---------------------------------------------------------------------------
# OTP button finder
# ---------------------------------------------------------------------------


class TestFindOtpButton:
    def test_found_via_primary_selector(self):
        page = MagicMock()
        btn = MagicMock()
        page.query_selector.return_value = btn

        assert _find_otp_button(page) is btn
        selector = page.query_selector.call_args[0][0]
        assert "emailed one-time code" in selector

    def test_found_via_text_scan_fallback(self):
        page = MagicMock()
        page.query_selector.return_value = None
        other = MagicMock()
        other.text_content.return_value = "Sign in with password"
        otp = MagicMock()
        otp.text_content.return_value = "Login with a One-Time Code"
        page.query_selector_all.return_value = [other, otp]

        assert _find_otp_button(page) is otp

    def test_not_found_returns_none(self):
        page = MagicMock()
        page.query_selector.return_value = None
        el = MagicMock()
        el.text_content.return_value = "Password login"
        page.query_selector_all.return_value = [el]

        assert _find_otp_button(page) is None


# ---------------------------------------------------------------------------
# Code input finder
# ---------------------------------------------------------------------------


class TestFindCodeInput:
    def test_found_via_primary_selector(self):
        page = MagicMock()
        inp = MagicMock()
        page.query_selector.return_value = inp
        assert _find_code_input(page) is inp

    def test_fallback_first_empty_visible_input(self):
        page = MagicMock()
        page.query_selector.return_value = None
        inp = MagicMock()
        inp.is_visible.return_value = True
        inp.input_value.return_value = ""
        page.query_selector_all.return_value = [inp]

        assert _find_code_input(page) is inp

    def test_fallback_skips_filled_and_hidden_inputs(self):
        page = MagicMock()
        page.query_selector.return_value = None
        filled = MagicMock()
        filled.is_visible.return_value = True
        filled.input_value.return_value = "already filled"
        hidden = MagicMock()
        hidden.is_visible.return_value = False
        hidden.input_value.return_value = ""
        empty = MagicMock()
        empty.is_visible.return_value = True
        empty.input_value.return_value = ""
        page.query_selector_all.return_value = [filled, hidden, empty]

        assert _find_code_input(page) is empty

    def test_not_found_returns_none(self):
        page = MagicMock()
        page.query_selector.return_value = None
        page.query_selector_all.return_value = []
        assert _find_code_input(page) is None


# ---------------------------------------------------------------------------
# Cookie banner dismissal
# ---------------------------------------------------------------------------


class TestDismissCookieBanner:
    def test_visible_accept_button_clicked(self):
        page = MagicMock()
        btn = MagicMock()
        btn.is_visible.return_value = True
        page.query_selector.return_value = btn

        assert _dismiss_cookie_banner(page) is True
        btn.click.assert_called_once()
        page.wait_for_timeout.assert_called_once_with(1000)

    def test_invisible_button_not_clicked(self):
        page = MagicMock()
        btn = MagicMock()
        btn.is_visible.return_value = False
        page.query_selector.return_value = btn

        assert _dismiss_cookie_banner(page) is False
        btn.click.assert_not_called()

    def test_no_banner_returns_false(self):
        page = MagicMock()
        page.query_selector.return_value = None
        assert _dismiss_cookie_banner(page) is False

    def test_selector_exception_swallowed(self):
        page = MagicMock()
        page.query_selector.side_effect = Exception("detached")
        assert _dismiss_cookie_banner(page) is False


# ---------------------------------------------------------------------------
# PageUp combobox
# ---------------------------------------------------------------------------


class TestFillPageupCombobox:
    def test_state_code_expanded_and_option_clicked(self):
        page = MagicMock()
        el = MagicMock()
        opt = MagicMock()
        opt.text_content.return_value = "Indiana"
        page.query_selector_all.return_value = [opt]

        assert _fill_pageup_combobox(page, el, "IN") is True
        page.keyboard.type.assert_called_once_with("Indiana", delay=50)
        opt.click.assert_called_once()

    def test_lowercase_state_code_expanded(self):
        page = MagicMock()
        el = MagicMock()
        page.query_selector_all.return_value = []

        _fill_pageup_combobox(page, el, "in")
        page.keyboard.type.assert_called_once_with("Indiana", delay=50)

    def test_unknown_two_letter_value_not_expanded(self):
        page = MagicMock()
        el = MagicMock()
        page.query_selector_all.return_value = []

        _fill_pageup_combobox(page, el, "ZZ")
        page.keyboard.type.assert_called_once_with("ZZ", delay=50)

    def test_longer_value_not_expanded(self):
        page = MagicMock()
        el = MagicMock()
        page.query_selector_all.return_value = []

        _fill_pageup_combobox(page, el, "Indianapolis")
        page.keyboard.type.assert_called_once_with("Indianapolis", delay=50)

    def test_no_options_falls_back_to_arrow_enter(self):
        page = MagicMock()
        el = MagicMock()
        page.query_selector_all.return_value = []

        assert _fill_pageup_combobox(page, el, "Indiana") is True
        pressed = [c[0][0] for c in page.keyboard.press.call_args_list]
        assert pressed == ["ArrowDown", "Enter"]

    def test_non_matching_options_fall_back_but_still_true(self):
        """Suspicion: always returns True even when no option matched,
        so callers cannot tell a blind arrow-down guess from a real match."""
        page = MagicMock()
        el = MagicMock()
        opt = MagicMock()
        opt.text_content.return_value = "Ohio"
        page.query_selector_all.return_value = [opt]

        assert _fill_pageup_combobox(page, el, "Indiana") is True
        opt.click.assert_not_called()
        pressed = [c[0][0] for c in page.keyboard.press.call_args_list]
        assert pressed == ["ArrowDown", "Enter"]


# ---------------------------------------------------------------------------
# Page loop branch coverage
# ---------------------------------------------------------------------------


class TestRunPageLoop:
    def _run(self, page=None, logger=None):
        return _run_page_loop(
            page or MagicMock(),
            _make_profile(),
            "Senior SRE",
            "Acme",
            "",
            "",
            "li_test123",
            logger or DecisionLogger(),
        )

    def test_submission_success_first_iteration(self, page_loop_patches):
        logger = DecisionLogger()
        with page_loop_patches(_detect_submission_success=True) as mocks:
            result = self._run(logger=logger)

        assert result == "submitted"
        mocks["_dismiss_cookie_banner"].assert_called_once()
        entries = logger.entries()
        assert entries[-1]["action"] == "submit"
        assert entries[-1]["target"] == "confirmation page"

    def test_rejection_returns_failure(self, page_loop_patches):
        logger = DecisionLogger()
        with page_loop_patches(_detect_rejection="spam filter"):
            result = self._run(logger=logger)

        assert result == "failed: spam filter"
        assert logger.entries()[-1]["action"] == "abort"

    def test_email_verification_routes_to_handler(self, page_loop_patches):
        page = MagicMock()
        logger = DecisionLogger()
        with page_loop_patches(
            _detect_email_verification=True,
            _handle_email_verification="failed: verification code rejected",
        ) as mocks:
            result = _run_page_loop(
                page,
                _make_profile(),
                "Senior SRE",
                "Acme",
                "",
                "",
                "li_test123",
                logger,
            )

        assert result == "failed: verification code rejected"
        mocks["_handle_email_verification"].assert_called_once()
        args = mocks["_handle_email_verification"].call_args[0]
        assert args[0] is page
        assert args[2] is logger

    def test_captcha_handled_at_most_twice(self, page_loop_patches):
        with page_loop_patches(_handle_captcha=True, _save_debug_snapshot=None) as mocks:
            result = self._run()

        # Two captcha rounds, then the constant snapshot stalls the loop
        assert mocks["_handle_captcha"].call_count == 2
        assert result == f"failed: stuck on same page state after {MAX_PAGE_ATTEMPTS} attempts"

    def test_max_page_attempts_exhaustion(self, page_loop_patches):
        logger = DecisionLogger()
        with page_loop_patches(_save_debug_snapshot=None) as mocks:
            result = self._run(logger=logger)

        assert result == f"failed: stuck on same page state after {MAX_PAGE_ATTEMPTS} attempts"
        # AI ran once per iteration until the stall counter tripped
        assert mocks["_ai_analyze_page"].call_count == MAX_PAGE_ATTEMPTS
        assert logger.entries()[-1]["action"] == "abort"


# ---------------------------------------------------------------------------
# DecisionLogger extras
# ---------------------------------------------------------------------------


class TestDecisionLoggerExtras:
    def test_default_field_values(self):
        logger = DecisionLogger()
        logger.log("click", "btn")

        entry = logger.entries()[0]
        assert entry["value"] == ""
        assert entry["reasoning"] == ""
        assert entry["confidence"] == "high"

    def test_unknown_confidence_recorded_verbatim(self):
        logger = DecisionLogger()
        logger.log("click", "btn", confidence="bogus")
        assert logger.entries()[0]["confidence"] == "bogus"

    def test_timestamp_format(self):
        import re as _re

        logger = DecisionLogger()
        logger.log("click", "btn")
        ts = logger.entries()[0]["timestamp"]
        assert _re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", ts)

    def test_entry_dict_shape(self):
        logger = DecisionLogger()
        logger.log("fill_field", "Email", "a@b.c", "match", "medium")
        entry = logger.entries()[0]
        assert set(entry) == {
            "step",
            "timestamp",
            "action",
            "target",
            "value",
            "reasoning",
            "confidence",
        }
