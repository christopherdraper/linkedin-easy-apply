"""Tests for profile parsing, formatting, and summary generation."""

from job_search_apply import (
    ApplicantProfile,
    _contact_value_for_label,
    _format_previous_employers,
    _profile_summary,
    already_applied,
)


class TestApplicantProfileFromDict:
    def test_full_profile(self):
        data = {
            "profile": {
                "personal": {
                    "full_name": "Jane Doe",
                    "email": "jane@example.com",
                    "phone": "555-1234",
                    "location": {"city": "Austin", "state": "TX", "zip_code": "73301"},
                    "linkedin_url": "https://linkedin.com/in/jane",
                },
                "work_authorization": {
                    "authorized_to_work_us": True,
                    "requires_visa_sponsorship": False,
                },
                "experience": {
                    "years_total": 7,
                    "current_title": "SRE",
                    "current_employer": "BigCorp",
                    "previous_employers": [
                        {"title": "DevOps", "employer": "StartUp", "industry": "Tech"}
                    ],
                    "specializations": ["SRE", "DevOps"],
                },
                "skills": {
                    "programming_languages": ["Python"],
                    "frameworks": ["Flask"],
                    "tools": ["Docker", "K8s"],
                },
                "documents": {"resume_path": "~/resume.pdf"},
                "screening_answers": {"salary": "120000"},
            }
        }
        p = ApplicantProfile.from_dict(data)
        assert p.full_name == "Jane Doe"
        assert p.email == "jane@example.com"
        assert p.city == "Austin"
        assert p.state == "TX"
        assert p.years_experience == 7
        assert p.current_title == "SRE"
        assert len(p.previous_employers) == 1
        assert p.skills == ["Python", "Flask", "Docker", "K8s"]
        assert p.authorized_to_work is True
        assert p.requires_sponsorship is False
        assert p.screening_answers["salary"] == "120000"

    def test_missing_fields_use_defaults(self):
        data = {
            "profile": {
                "personal": {"full_name": "X", "email": "x@x.com", "phone": "0"},
                "documents": {"resume_path": "r.pdf"},
            }
        }
        p = ApplicantProfile.from_dict(data)
        assert p.years_experience is None
        assert p.current_title is None
        assert p.previous_employers == []
        assert p.skills == []
        assert p.screening_answers == {}
        assert p.authorized_to_work is True  # default

    def test_skills_concatenation(self):
        data = {
            "profile": {
                "personal": {"full_name": "X", "email": "x@x.com", "phone": "0"},
                "skills": {
                    "programming_languages": ["A", "B"],
                    "frameworks": ["C"],
                    "tools": ["D", "E"],
                },
                "documents": {"resume_path": "r.pdf"},
            }
        }
        p = ApplicantProfile.from_dict(data)
        assert p.skills == ["A", "B", "C", "D", "E"]


class TestFormatPreviousEmployers:
    def test_empty(self, profile):
        profile.previous_employers = []
        assert _format_previous_employers(profile) == "none listed"

    def test_single_with_industry(self, profile):
        profile.previous_employers = [{"title": "SRE", "employer": "Acme", "industry": "Tech"}]
        assert _format_previous_employers(profile) == "SRE at Acme (Tech)"

    def test_multiple(self, profile):
        result = _format_previous_employers(profile)
        assert "DevSecOps Engineer at GovTech" in result
        assert "Linux Engineer at State College" in result
        assert "; " in result

    def test_missing_industry(self, profile):
        profile.previous_employers = [{"title": "Dev", "employer": "Co"}]
        assert _format_previous_employers(profile) == "Dev at Co"


class TestProfileSummary:
    def test_contains_key_fields(self, profile):
        summary = _profile_summary(profile)
        assert "Test User" in summary
        assert "test@example.com" in summary
        assert "Senior Site Reliability Engineer" in summary
        assert "Acme Corp" in summary
        assert "Indianapolis" in summary
        assert "Python" in summary
        assert "9" in summary

    def test_previous_employers_included(self, profile):
        summary = _profile_summary(profile)
        assert "GovTech" in summary
        assert "State College" in summary


class TestContactValueForLabel:
    def test_phone(self, profile):
        assert _contact_value_for_label("phone number", profile) == "+1-555-123-4567"
        assert _contact_value_for_label("mobile", profile) == "+1-555-123-4567"
        assert _contact_value_for_label("telephone", profile) == "+1-555-123-4567"

    def test_city(self, profile):
        assert _contact_value_for_label("city", profile) == "Indianapolis"

    def test_state_abbreviation(self, profile):
        result = _contact_value_for_label("state", profile)
        assert result == "IN"

    def test_state_full_name(self, profile):
        result = _contact_value_for_label("state full name", profile)
        assert result == "Indiana"

    def test_zip(self, profile):
        assert _contact_value_for_label("zip code", profile) == "46201"
        assert _contact_value_for_label("postal code", profile) == "46201"

    def test_email(self, profile):
        assert _contact_value_for_label("email address", profile) == "test@example.com"

    def test_linkedin(self, profile):
        assert (
            _contact_value_for_label("linkedin url", profile)
            == "https://www.linkedin.com/in/testuser/"
        )

    def test_github(self, profile):
        assert _contact_value_for_label("github profile", profile) == "https://github.com/testuser"

    def test_no_match(self, profile):
        assert _contact_value_for_label("favorite color", profile) is None

    def test_missing_field(self, profile):
        profile.github_url = None
        assert _contact_value_for_label("github", profile) is None


class TestAlreadyApplied:
    def test_empty_log(self):
        assert already_applied([]) == set()

    def test_submitted_entries(self):
        log = [
            {"url": "https://a.com", "status": "submitted"},
            {"url": "https://b.com", "status": "submitted (unconfirmed — check LinkedIn)"},
            {"url": "https://c.com", "status": "failed: form stuck"},
        ]
        result = already_applied(log)
        assert "https://a.com" in result
        assert "https://b.com" in result
        assert "https://c.com" not in result

    def test_missing_url(self):
        log = [{"status": "submitted"}]
        result = already_applied(log)
        assert len(result) == 0

    def test_mixed_statuses(self):
        log = [
            {"url": "https://a.com", "status": "submitted"},
            {"url": "https://b.com", "status": "aborted: injection"},
            {"url": "https://c.com", "status": "dry_run"},
            {"url": "https://d.com", "status": "failed: no button"},
        ]
        result = already_applied(log)
        assert result == {"https://a.com"}
