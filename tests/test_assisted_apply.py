"""Tests for Q2 assisted apply module."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assisted_apply_mcp import (  # noqa: E402
    DecisionLogger,
    _detect_submission_success,
    _execute_action_plan,
    _match_field_to_profile,
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

        result = _execute_action_plan(self._make_action(), page, profile, "/tmp/resume.pdf", logger)
        assert result is True
        assert logger.entries()[0]["action"] == "fill_field"

    def test_click_action(self):
        page = MagicMock()
        el = MagicMock()
        page.query_selector.return_value = el
        profile = _make_profile()
        logger = DecisionLogger()

        result = _execute_action_plan(
            self._make_action(action="click", target="Next button"), page, profile, "", logger
        )
        assert result is True
        assert logger.entries()[0]["action"] == "click"

    def test_skip_action(self):
        page = MagicMock()
        profile = _make_profile()
        logger = DecisionLogger()

        result = _execute_action_plan(
            self._make_action(action="skip", target="Unknown field"), page, profile, "", logger
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
        _execute_action_plan(action, page, profile, "/tmp/resume.pdf", logger)

        entry = logger.entries()[0]
        assert entry["value"] == "matt@example.com"
        assert entry["confidence"] == "high"
