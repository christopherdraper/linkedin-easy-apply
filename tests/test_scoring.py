"""Tests for keyword-based job scoring."""

from job_search_apply import score_job


class TestScoreJob:
    def test_high_skill_match(self, profile, job):
        result = score_job(job, profile)
        assert result["match_score"] >= 0.5
        assert "kubernetes" in result["matched_skills"]
        assert "terraform" in result["matched_skills"]

    def test_no_skill_match(self, profile):
        job = {
            "title": "Hairdresser",
            "description": "Looking for an experienced hairdresser with salon experience.",
        }
        result = score_job(job, profile)
        assert result["match_score"] < 0.3
        assert len(result["matched_skills"]) == 0

    def test_title_match_boosts_score(self, profile):
        job_good_title = {
            "title": "Site Reliability Engineering Lead",
            "description": "Generic job description with no skill keywords.",
        }
        job_bad_title = {
            "title": "Marketing Manager",
            "description": "Generic job description with no skill keywords.",
        }
        score_good = score_job(job_good_title, profile)["match_score"]
        score_bad = score_job(job_bad_title, profile)["match_score"]
        assert score_good > score_bad

    def test_empty_description(self, profile):
        job = {"title": "DevOps Engineer", "description": ""}
        result = score_job(job, profile)
        # Should still get some score from title match
        assert isinstance(result["match_score"], float)
        assert 0.0 <= result["match_score"] <= 1.0

    def test_html_stripped(self, profile):
        job = {
            "title": "DevOps Engineer",
            "description": "<div>Need <b>Kubernetes</b> and <em>Terraform</em> skills</div>",
        }
        result = score_job(job, profile)
        assert "kubernetes" in result["matched_skills"]
        assert "terraform" in result["matched_skills"]

    def test_score_between_0_and_1(self, profile, job):
        result = score_job(job, profile)
        assert 0.0 <= result["match_score"] <= 1.0

    def test_result_structure(self, profile, job):
        result = score_job(job, profile)
        assert "match_score" in result
        assert "matched_skills" in result
        assert "reasoning" in result
        assert "deal_breakers" in result
