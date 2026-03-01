"""Shared fixtures for unit tests."""

# Allow importing from the parent directory
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_search_apply import ApplicantProfile  # noqa: E402


@pytest.fixture
def profile():
    """A realistic applicant profile for testing."""
    return ApplicantProfile(
        full_name="Test User",
        email="test@example.com",
        phone="+1-555-123-4567",
        resume_path="~/resume.pdf",
        linkedin_url="https://www.linkedin.com/in/testuser/",
        github_url="https://github.com/testuser",
        years_experience=9,
        current_title="Senior Site Reliability Engineer",
        current_employer="Acme Corp",
        previous_employers=[
            {"title": "DevSecOps Engineer", "employer": "GovTech", "industry": "Government IT"},
            {"title": "Linux Engineer", "employer": "State College", "industry": "Education"},
        ],
        city="Indianapolis",
        state="IN",
        zip_code="46201",
        skills=["Python", "Bash", "Terraform", "Ansible", "Kubernetes", "Docker", "AWS", "GCP"],
        specializations=["Site Reliability Engineering", "DevOps", "Infrastructure Automation"],
        authorized_to_work=True,
        requires_sponsorship=False,
        screening_answers={
            "salary": "150000",
            "desired salary": "150000",
            "years of experience with kubernetes": "5",
            "years of experience with python": "9",
            "devops": "9",
            "willing to travel": "10",
            "how did you hear": "LinkedIn",
            "legally authorized": "Yes",
            "require sponsorship": "No",
        },
    )


@pytest.fixture
def job():
    """A realistic job posting dict."""
    return {
        "id": "li_abc123",
        "title": "Senior DevOps Engineer",
        "company": "TechCo",
        "location": "Remote",
        "url": "https://www.linkedin.com/jobs/view/123",
        "description": (
            "We are looking for a Senior DevOps Engineer with experience in "
            "Kubernetes, Terraform, AWS, Docker, and CI/CD pipelines. "
            "Must have 5+ years of experience with Linux and infrastructure automation."
        ),
    }


@pytest.fixture
def compat():
    """A realistic compatibility/scoring dict."""
    return {
        "match_score": 0.85,
        "reasoning": "Strong match for DevOps skills",
        "matched_skills": ["Kubernetes", "Terraform", "AWS", "Docker"],
        "deal_breakers": [],
    }
