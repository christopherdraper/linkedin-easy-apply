"""Tests for external ATS application support."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_search_apply import (  # noqa: E402
    _classify_page,
    _detect_success_or_confirmation,
    _extract_page_snapshot,
    _find_navigation_button,
    _is_login_wall,
    _parse_job_cards,
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
        page.evaluate.return_value = "we are looking for a senior engineer"
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
