"""Tests for cover letter generation and application notes fallbacks."""

from job_search_apply import _basic_cover_letter, _basic_notes


class TestBasicCoverLetter:
    def test_without_template(self, profile, job):
        profile.cover_letter_template = None
        result = _basic_cover_letter(job, profile)
        assert "Senior DevOps Engineer" in result
        assert "TechCo" in result
        assert "Test User" in result
        assert "test@example.com" in result

    def test_with_template(self, profile, job):
        profile.cover_letter_template = (
            "{DATE}\n{COMPANY}\nRE: {JOB_TITLE}\n\n"
            "Dear {HIRING_MANAGER_NAME},\nI want this job.\n"
            "---\nTEMPLATE INSTRUCTIONS FOR AI:\nFill in the placeholders."
        )
        result = _basic_cover_letter(job, profile)
        assert "TechCo" in result
        assert "Senior DevOps Engineer" in result
        assert "{COMPANY}" not in result
        assert "{JOB_TITLE}" not in result
        # AI instructions should be stripped
        assert "TEMPLATE INSTRUCTIONS" not in result

    def test_years_in_generic_letter(self, profile, job):
        profile.cover_letter_template = None
        result = _basic_cover_letter(job, profile)
        assert "9" in result  # years of experience


class TestBasicNotes:
    def test_contains_job_info(self, job, compat):
        result = _basic_notes(job, compat)
        assert "Senior DevOps Engineer" in result
        assert "TechCo" in result
        assert "https://www.linkedin.com/jobs/view/123" in result

    def test_contains_score(self, job, compat):
        result = _basic_notes(job, compat)
        assert "0.85" in result

    def test_contains_skills(self, job, compat):
        result = _basic_notes(job, compat)
        assert "Kubernetes" in result

    def test_contains_description_snippet(self, job, compat):
        result = _basic_notes(job, compat)
        assert "Kubernetes" in result or "DevOps" in result

    def test_truncates_long_description(self, job, compat):
        job["description"] = "x " * 500  # 1000 chars
        result = _basic_notes(job, compat)
        # Description should be truncated with ellipsis
        assert len(result) < 2000
