"""Tests for external ATS application support."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_search_apply import (  # noqa: E402
    ApplicantProfile,
    _2captcha_solve,
    _attempt_ats_login,
    _classify_page,
    _detect_captcha,
    _detect_success_or_confirmation,
    _extract_page_snapshot,
    _fill_registration_form,
    _find_navigation_button,
    _generate_ats_password,
    _get_domain,
    _inject_captcha_token,
    _is_login_wall,
    _load_ats_accounts,
    _parse_job_cards,
    _save_ats_account,
    _solve_captcha,
)

# ---------------------------------------------------------------------------
# _is_login_wall tests
# ---------------------------------------------------------------------------


class TestIsLoginWall:
    def test_login_url_detected(self):
        page = MagicMock()
        page.url = "https://company.wd5.myworkdayjobs.com/login"
        page.query_selector.return_value = None  # no guest link
        assert _is_login_wall(page) is True

    def test_signin_url_detected(self):
        page = MagicMock()
        page.url = "https://jobs.lever.co/company/signin"
        page.query_selector.return_value = None
        assert _is_login_wall(page) is True

    def test_normal_url_no_password(self):
        page = MagicMock()
        page.url = "https://jobs.greenhouse.io/company/apply/123"
        page.query_selector.return_value = None  # no password field
        # First evaluate: JS login indicator check returns False
        # page.frames should not have extra frames
        page.frames = [page.main_frame]
        page.evaluate.return_value = False
        assert _is_login_wall(page) is False

    def test_password_field_detected(self):
        page = MagicMock()
        page.url = "https://company.com/apply"
        # First query_selector call: no guest link. Second: password field found.
        password_el = MagicMock()
        password_el.is_visible.return_value = True

        def side_effect(selector):
            if "guest" in selector.lower() or "without" in selector.lower():
                return None
            if "password" in selector:
                return password_el
            return None

        page.query_selector.side_effect = side_effect
        assert _is_login_wall(page) is True

    def test_guest_link_bypasses_login(self):
        page = MagicMock()
        page.url = "https://company.wd5.myworkdayjobs.com/login"
        guest_btn = MagicMock()
        page.query_selector.return_value = guest_btn  # guest link found
        assert _is_login_wall(page) is False

    def test_sign_in_to_apply_text(self):
        page = MagicMock()
        page.url = "https://company.com/careers/apply"
        page.query_selector.return_value = None
        page.evaluate.return_value = "please sign in to apply for this position"
        assert _is_login_wall(page) is True

    def test_bmc_from_device_bypasses_login(self):
        """BMC-style 'From Device' option should bypass via resume upload."""
        page = MagicMock()
        page.url = "https://jobs.bmc.com/login"
        from_device_btn = MagicMock()
        from_device_btn.is_visible.return_value = True

        def side_effect(selector):
            if "From Device" in selector:
                return from_device_btn
            return None

        page.query_selector.side_effect = side_effect

        profile = MagicMock(spec=ApplicantProfile)
        profile.resume_path = "/tmp/fake_resume.pdf"

        with patch("job_search_apply.Path") as mock_path:
            mock_path.return_value.expanduser.return_value.exists.return_value = True
            mock_path.return_value.expanduser.return_value.name = "fake_resume.pdf"
            mock_path.return_value.expanduser.return_value.__str__ = lambda s: (
                "/tmp/fake_resume.pdf"
            )
            # Mock file chooser context manager
            fc_mock = MagicMock()
            page.expect_file_chooser.return_value.__enter__ = MagicMock(return_value=fc_mock)
            page.expect_file_chooser.return_value.__exit__ = MagicMock(return_value=False)
            fc_mock.value = MagicMock()

            assert _is_login_wall(page, profile) is False

    def test_login_wall_with_stored_account(self):
        """Should attempt ATS login when stored credentials exist."""
        page = MagicMock()
        page.url = "https://jobs.bmc.com/login"
        page.query_selector.return_value = None  # no guest link

        profile = MagicMock()
        profile.auto_create_accounts = True

        with patch("job_search_apply._attempt_ats_login", return_value=True):
            assert _is_login_wall(page, profile) is False


# ---------------------------------------------------------------------------
# ATS account management tests
# ---------------------------------------------------------------------------


class TestGenerateAtsPassword:
    def test_length_and_requirements(self):
        pw = _generate_ats_password()
        assert len(pw) == 16
        assert any(c.isupper() for c in pw)
        assert any(c.islower() for c in pw)
        assert any(c.isdigit() for c in pw)
        assert any(c in "!@#$%&*" for c in pw)

    def test_unique_each_call(self):
        passwords = {_generate_ats_password() for _ in range(10)}
        assert len(passwords) == 10


class TestGetDomain:
    def test_extracts_domain(self):
        assert _get_domain("https://jobs.bmc.com/apply/123") == "jobs.bmc.com"

    def test_handles_port(self):
        assert _get_domain("http://localhost:3000/apply") == "localhost:3000"


class TestAtsAccountStorage:
    def test_save_and_load(self, tmp_path):
        acct_file = tmp_path / "ats_accounts.json"
        with patch("job_search_apply.ATS_ACCOUNTS_FILE", acct_file):
            _save_ats_account("jobs.bmc.com", "test@example.com", "SecretPass1!")
            accounts = _load_ats_accounts()
            assert "jobs.bmc.com" in accounts
            assert accounts["jobs.bmc.com"]["email"] == "test@example.com"

    def test_load_missing_file(self, tmp_path):
        with patch("job_search_apply.ATS_ACCOUNTS_FILE", tmp_path / "missing.json"):
            assert _load_ats_accounts() == {}


class TestFillRegistrationForm:
    def test_fills_name_email_password(self):
        page = MagicMock()
        profile = MagicMock(spec=ApplicantProfile)
        profile.full_name = "John Doe"
        profile.email = "john@example.com"
        profile.phone = "+1-555-0100"

        first_f = MagicMock()
        last_f = MagicMock()
        email_f = MagicMock()
        pass_f = MagicMock()
        pass_f.is_visible.return_value = True

        def qs(selector):
            if "first" in selector.lower():
                return first_f
            if "last" in selector.lower():
                return last_f
            if "email" in selector.lower():
                return email_f
            if "tel" in selector.lower() or "phone" in selector.lower():
                return None
            if "checkbox" in selector.lower():
                return None
            return None

        page.query_selector.side_effect = qs
        page.query_selector_all.return_value = [pass_f]

        filled = _fill_registration_form(page, profile, "TestPass1!")
        assert filled >= 3  # first, last, email, password
        first_f.fill.assert_called_with("John")
        last_f.fill.assert_called_with("Doe")
        email_f.fill.assert_called_with("john@example.com")


class TestAttemptAtsLogin:
    def test_no_stored_account(self):
        page = MagicMock()
        with patch("job_search_apply._load_ats_accounts", return_value={}):
            assert _attempt_ats_login(page, "unknown.com") is False

    def test_login_with_stored_creds(self):
        page = MagicMock()
        page.url = "https://jobs.example.com/dashboard"  # redirected after login

        email_f = MagicMock()
        pass_f = MagicMock()
        login_btn = MagicMock()

        def qs(selector):
            if "email" in selector.lower() or "user" in selector.lower():
                return email_f
            if "password" in selector:
                return pass_f
            if "submit" in selector.lower() or "log in" in selector.lower():
                return login_btn
            return None

        page.query_selector.side_effect = qs

        accounts = {"jobs.example.com": {"email": "me@test.com", "password": "pw123"}}
        with patch("job_search_apply._load_ats_accounts", return_value=accounts):
            result = _attempt_ats_login(page, "jobs.example.com")
            assert result is True
            email_f.fill.assert_called_with("me@test.com")
            pass_f.fill.assert_called_with("pw123")


# ---------------------------------------------------------------------------
# _detect_success_or_confirmation tests
# ---------------------------------------------------------------------------


class TestDetectSuccessOrConfirmation:
    def test_confirmation_url(self):
        page = MagicMock()
        page.url = "https://company.com/apply/confirmation"
        assert _detect_success_or_confirmation(page, "") is True

    def test_thank_you_url(self):
        page = MagicMock()
        page.url = "https://jobs.lever.co/company/thank-you"
        assert _detect_success_or_confirmation(page, "") is True

    def test_success_url(self):
        page = MagicMock()
        page.url = "https://company.com/apply/success"
        assert _detect_success_or_confirmation(page, "") is True

    def test_confirmation_text_in_snapshot(self):
        page = MagicMock()
        page.url = "https://company.com/apply/step4"
        snapshot = "[button] text='Done'\nThank you for applying to our position"
        assert _detect_success_or_confirmation(page, snapshot) is True

    def test_application_submitted_text(self):
        page = MagicMock()
        page.url = "https://company.com/apply"
        snapshot = "Your application submitted successfully"
        assert _detect_success_or_confirmation(page, snapshot) is True

    def test_no_confirmation(self):
        page = MagicMock()
        page.url = "https://company.com/apply/step2"
        snapshot = "[input:text] label='First Name'\n[input:text] label='Last Name'"
        assert _detect_success_or_confirmation(page, snapshot) is False


# ---------------------------------------------------------------------------
# _classify_page tests
# ---------------------------------------------------------------------------


class TestClassifyPage:
    def test_fallback_when_ai_unavailable(self):
        with patch("job_search_apply._AI_AVAILABLE", False):
            result = _classify_page("some snapshot", "https://example.com")
            assert result["page_type"] == "form"
            assert result["has_form_fields"] is True

    def test_fallback_on_empty_snapshot(self):
        result = _classify_page("", "https://example.com")
        assert result["page_type"] == "form"

    @patch("job_search_apply._get_ai_client")
    @patch("job_search_apply._AI_AVAILABLE", True)
    def test_classifies_login_page(self, mock_client):
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text='{"page_type": "login", "has_required_login": true, "has_file_upload": false, "has_form_fields": false, "notes": "login page"}'
            )
        ]
        mock_client.return_value.messages.create.return_value = mock_response

        result = _classify_page("[input:email] [input:password]", "https://company.com/login")
        assert result["page_type"] == "login"
        assert result["has_required_login"] is True

    @patch("job_search_apply._get_ai_client")
    @patch("job_search_apply._AI_AVAILABLE", True)
    def test_classifies_form_page(self, mock_client):
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text='{"page_type": "form", "has_required_login": false, "has_file_upload": true, "has_form_fields": true, "notes": "application form"}'
            )
        ]
        mock_client.return_value.messages.create.return_value = mock_response

        result = _classify_page("[input:text] label='Name'", "https://company.com/apply")
        assert result["page_type"] == "form"
        assert result["has_file_upload"] is True

    @patch("job_search_apply._get_ai_client")
    @patch("job_search_apply._AI_AVAILABLE", True)
    def test_handles_ai_exception(self, mock_client):
        mock_client.return_value.messages.create.side_effect = Exception("API error")
        result = _classify_page("some snapshot", "https://example.com")
        assert result["page_type"] == "form"  # safe fallback


# ---------------------------------------------------------------------------
# _find_navigation_button tests
# ---------------------------------------------------------------------------


class TestFindNavigationButton:
    def test_finds_submit_button(self):
        page = MagicMock()
        submit_btn = MagicMock()
        submit_btn.is_visible.return_value = True

        def qs(selector):
            if "Submit Application" in selector:
                return submit_btn
            return None

        page.query_selector.side_effect = qs
        role, el = _find_navigation_button(page)
        assert role == "submit"
        assert el is submit_btn

    def test_finds_next_button(self):
        page = MagicMock()
        next_btn = MagicMock()
        next_btn.is_visible.return_value = True

        def qs(selector):
            if "Next" in selector and "button:" in selector:
                return next_btn
            return None

        page.query_selector.side_effect = qs
        role, el = _find_navigation_button(page)
        assert role == "next"

    def test_no_button_found(self):
        page = MagicMock()
        page.query_selector.return_value = None
        role, el = _find_navigation_button(page)
        assert role == "none"
        assert el is None

    def test_skips_login_submit(self):
        page = MagicMock()
        login_btn = MagicMock()
        login_btn.is_visible.return_value = True
        login_btn.inner_text.return_value = "Sign in"

        def qs(selector):
            if "button[type='submit']" in selector:
                return login_btn
            return None

        page.query_selector.side_effect = qs
        # It should skip "Sign in" buttons and return none
        role, el = _find_navigation_button(page)
        assert role == "none"
        assert el is None


# ---------------------------------------------------------------------------
# _parse_job_cards tests (updated for apply_type)
# ---------------------------------------------------------------------------


class TestParseJobCardsApplyType:
    def _make_card(self, title, company, has_easy_apply=True):
        card = MagicMock()
        title_el = MagicMock()
        title_el.inner_text.return_value = title
        title_el.evaluate.return_value = f"https://linkedin.com/jobs/{title.replace(' ', '-')}"
        company_el = MagicMock()
        company_el.inner_text.return_value = company
        location_el = MagicMock()
        location_el.inner_text.return_value = "Remote"

        footer_item = MagicMock()
        footer_item.inner_text.return_value = "Easy Apply" if has_easy_apply else "Applied"

        card.query_selector.side_effect = lambda s: {
            "a.job-card-list__title--link": title_el,
            "div.artdeco-entity-lockup__subtitle": company_el,
            "div.artdeco-entity-lockup__caption": location_el,
        }.get(s)
        card.query_selector_all.return_value = [footer_item]
        return card

    def test_easy_apply_card_tagged(self):
        page = MagicMock()
        page.query_selector_all.return_value = [self._make_card("SRE", "TechCo", True)]
        jobs = _parse_job_cards(page)
        assert len(jobs) == 1
        assert jobs[0]["apply_type"] == "easy_apply"

    def test_external_card_tagged(self):
        page = MagicMock()
        page.query_selector_all.return_value = [self._make_card("SRE", "TechCo", False)]
        jobs = _parse_job_cards(page)
        assert len(jobs) == 1
        assert jobs[0]["apply_type"] == "external"

    def test_mixed_cards(self):
        page = MagicMock()
        page.query_selector_all.return_value = [
            self._make_card("SRE", "Company A", True),
            self._make_card("DevOps", "Company B", False),
            self._make_card("Platform Eng", "Company C", True),
        ]
        jobs = _parse_job_cards(page)
        assert len(jobs) == 3
        types = [j["apply_type"] for j in jobs]
        assert types == ["easy_apply", "external", "easy_apply"]


# ---------------------------------------------------------------------------
# _extract_page_snapshot tests
# ---------------------------------------------------------------------------


class TestExtractPageSnapshot:
    def test_returns_string(self):
        page = MagicMock()
        page.evaluate.return_value = '[input:text] label="Name" required'
        result = _extract_page_snapshot(page)
        assert isinstance(result, str)
        assert "Name" in result

    def test_truncates_to_max_chars(self):
        page = MagicMock()
        page.evaluate.return_value = "x" * 10000
        result = _extract_page_snapshot(page, max_chars=100)
        assert len(result) <= 100

    def test_handles_evaluate_exception(self):
        page = MagicMock()
        page.evaluate.side_effect = Exception("JS error")
        result = _extract_page_snapshot(page)
        assert result == ""

    def test_handles_none_return(self):
        page = MagicMock()
        page.evaluate.return_value = None
        result = _extract_page_snapshot(page)
        assert result == ""


# ---------------------------------------------------------------------------
# _detect_captcha tests
# ---------------------------------------------------------------------------


class TestDetectCaptcha:
    def test_returns_recaptchav2(self):
        page = MagicMock()
        page.evaluate.return_value = {"type": "recaptchav2", "sitekey": "abc123"}
        result = _detect_captcha(page)
        assert result is not None
        assert result["type"] == "recaptchav2"
        assert result["sitekey"] == "abc123"

    def test_returns_hcaptcha(self):
        page = MagicMock()
        page.evaluate.return_value = {"type": "hcaptcha", "sitekey": "hc-key"}
        result = _detect_captcha(page)
        assert result["type"] == "hcaptcha"

    def test_returns_none_when_no_captcha(self):
        page = MagicMock()
        page.evaluate.return_value = None
        result = _detect_captcha(page)
        assert result is None

    def test_returns_none_on_exception(self):
        page = MagicMock()
        page.evaluate.side_effect = Exception("context destroyed")
        result = _detect_captcha(page)
        assert result is None

    def test_returns_unknown_for_text_detection(self):
        page = MagicMock()
        page.evaluate.return_value = {"type": "unknown", "sitekey": ""}
        result = _detect_captcha(page)
        assert result["type"] == "unknown"
        assert result["sitekey"] == ""


# ---------------------------------------------------------------------------
# _solve_captcha tests
# ---------------------------------------------------------------------------


class TestSolveCaptcha:
    def test_no_sitekey_returns_false(self):
        page = MagicMock()
        info = {"type": "recaptchav2", "sitekey": ""}
        assert _solve_captcha(page, info, "api-key") is False

    def test_unknown_type_returns_false(self):
        page = MagicMock()
        info = {"type": "unknown", "sitekey": ""}
        assert _solve_captcha(page, info, "api-key") is False

    @patch("job_search_apply._inject_captcha_token", return_value=True)
    @patch("job_search_apply._2captcha_solve", return_value="solved-token-123")
    def test_2captcha_success(self, mock_solve, mock_inject):
        page = MagicMock()
        info = {"type": "recaptchav2", "sitekey": "site-key-abc"}
        result = _solve_captcha(page, info, "api-key", service="2captcha")
        assert result is True
        mock_solve.assert_called_once_with(
            "api-key",
            "https://2captcha.com",
            "recaptchav2",
            "site-key-abc",
            page.url,
        )
        mock_inject.assert_called_once_with(page, "recaptchav2", "solved-token-123")

    @patch("job_search_apply._inject_captcha_token")
    @patch("job_search_apply._capsolver_solve", return_value="cs-token")
    def test_capsolver_success(self, mock_solve, mock_inject):
        mock_inject.return_value = True
        page = MagicMock()
        info = {"type": "hcaptcha", "sitekey": "hc-key"}
        result = _solve_captcha(page, info, "api-key", service="capsolver")
        assert result is True
        mock_solve.assert_called_once_with("api-key", "hcaptcha", "hc-key", page.url)

    @patch("job_search_apply._2captcha_solve", return_value=None)
    def test_solve_failure_returns_false(self, mock_solve):
        page = MagicMock()
        info = {"type": "recaptchav2", "sitekey": "key"}
        assert _solve_captcha(page, info, "api-key") is False

    @patch("job_search_apply._2captcha_solve", side_effect=Exception("network error"))
    def test_solve_exception_returns_false(self, mock_solve):
        page = MagicMock()
        info = {"type": "recaptchav2", "sitekey": "key"}
        assert _solve_captcha(page, info, "api-key") is False


# ---------------------------------------------------------------------------
# _inject_captcha_token tests
# ---------------------------------------------------------------------------


class TestInjectCaptchaToken:
    def test_recaptcha_injection(self):
        page = MagicMock()
        result = _inject_captcha_token(page, "recaptchav2", "token-abc")
        assert result is True
        page.evaluate.assert_called_once()
        # Verify token was passed as second arg
        args = page.evaluate.call_args
        assert args[0][1] == "token-abc"

    def test_hcaptcha_injection(self):
        page = MagicMock()
        result = _inject_captcha_token(page, "hcaptcha", "hc-token")
        assert result is True
        page.evaluate.assert_called_once()

    def test_turnstile_injection(self):
        page = MagicMock()
        result = _inject_captcha_token(page, "turnstile", "cf-token")
        assert result is True

    def test_unknown_type_returns_false(self):
        page = MagicMock()
        result = _inject_captcha_token(page, "unknown", "token")
        assert result is False

    def test_exception_returns_false(self):
        page = MagicMock()
        page.evaluate.side_effect = Exception("JS error")
        result = _inject_captcha_token(page, "recaptchav2", "token")
        assert result is False


# ---------------------------------------------------------------------------
# _2captcha_solve tests
# ---------------------------------------------------------------------------


class Test2CaptchaSolve:
    @patch("urllib.request.urlopen")
    def test_submit_error(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"status": 0, "request": "ERROR_WRONG_USER_KEY"}'
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        result = _2captcha_solve("bad-key", "https://2captcha.com", "recaptchav2", "sk", "url")
        assert result is None

    @patch("time.sleep")
    @patch("urllib.request.urlopen")
    def test_success_after_poll(self, mock_urlopen, mock_sleep):
        submit_resp = MagicMock()
        submit_resp.read.return_value = b'{"status": 1, "request": "12345"}'
        submit_resp.__enter__ = lambda s: s
        submit_resp.__exit__ = MagicMock(return_value=False)

        poll_resp = MagicMock()
        poll_resp.read.return_value = b'{"status": 1, "request": "solved-token"}'
        poll_resp.__enter__ = lambda s: s
        poll_resp.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [submit_resp, poll_resp]
        result = _2captcha_solve("key", "https://2captcha.com", "recaptchav2", "sk", "url")
        assert result == "solved-token"
