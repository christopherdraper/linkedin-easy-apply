"""Shared fixtures for unit tests."""

# Allow importing from the parent directory
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

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


@pytest.fixture
def make_page():
    """Factory for a minimal mock Playwright page.

    The returned page survives a full pass of _navigate_external_form:
    no selectors match, no frames beyond main, empty evaluate results.
    Override attributes per test (page.url, page.evaluate.return_value,
    page.query_selector.side_effect, ...).
    """

    def _factory(url="https://test.com/apply", body_text=""):
        page = MagicMock()
        page.url = url
        page.query_selector_all.return_value = []
        page.query_selector.return_value = None
        page.frames = [page.main_frame]
        page.content.return_value = "<html><body>Test</body></html>"
        page.evaluate.return_value = body_text
        return page

    return _factory


@pytest.fixture
def ai_client():
    """Patch job_search_apply's AI client with a mock returning canned text.

    Usage:
        with ai_client('{"match_score": 0.9}') as mock_client:
            ...
    The mock response mimics anthropic messages.create: content[0].text
    plus usage.input_tokens / usage.output_tokens.
    """

    @contextmanager
    def _patched(response_text, input_tokens=100, output_tokens=50):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=response_text)]
        mock_response.usage.input_tokens = input_tokens
        mock_response.usage.output_tokens = output_tokens
        with (
            patch("job_search_apply._AI_AVAILABLE", True),
            patch("job_search_apply._get_ai_client") as mock_client,
        ):
            mock_client.return_value.messages.create.return_value = mock_response
            yield mock_client

    return _patched


@pytest.fixture
def playwright_ctx():
    """Patch the browser-context bundle so submit_* entry points run without
    a real browser. Yields (browser_mock, context_mock, page)."""

    @contextmanager
    def _patched(page):
        context = MagicMock()
        context.pages = [page]
        browser = MagicMock()
        with (
            patch("job_search_apply._stealth_playwright"),
            patch("job_search_apply._playwright_context") as mock_ctx,
            patch("job_search_apply._ensure_logged_in"),
            patch("job_search_apply._wait_and_dismiss_cookies"),
        ):
            mock_ctx.return_value = (browser, context, page, True)
            yield browser, context, page

    return _patched


@pytest.fixture
def page_loop_patches():
    """The Q2 _run_page_loop collaborator bundle with passing defaults.

    Yields a dict of named mocks; override return values per test before
    invoking _run_page_loop.
    """

    @contextmanager
    def _patched(**overrides):
        defaults = {
            "_detect_submission_success": False,
            "_detect_email_verification": False,
            "_detect_rejection": None,
            "_handle_captcha": False,
            "_handle_login_wall": False,
            "_get_page_text_snapshot": "snapshot",
            "_fix_corrupted_fields": None,
            "_ai_analyze_page": None,
            "_handle_no_actions": "failed: no actions",
            "_dismiss_cookie_banner": None,
            "_clear_errored_uploads": None,
        }
        defaults.update(overrides)
        patchers = {
            name: patch(f"assisted_apply_mcp.{name}", return_value=value)
            for name, value in defaults.items()
        }
        mocks = {}
        try:
            for name, p in patchers.items():
                mocks[name] = p.start()
            yield mocks
        finally:
            for p in patchers.values():
                p.stop()

    return _patched


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Redirect every file-path constant to tmp_path so no test can touch
    ~/.local/share/job-apply. Returns the tmp_path."""
    import dashboard
    import job_search_apply
    import jobapply.browser

    monkeypatch.setattr(jobapply.browser, "SESSION_FILE", tmp_path / "session.json")
    monkeypatch.setattr(jobapply.browser, "CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(job_search_apply, "DATA_DIR", tmp_path)
    monkeypatch.setattr(job_search_apply, "LOG_FILE", tmp_path / "applications.json")
    monkeypatch.setattr(job_search_apply, "SEARCH_LOG_FILE", tmp_path / "search_log.json")
    monkeypatch.setattr(job_search_apply, "COVER_LETTER_DIR", tmp_path / "cover-letters")
    monkeypatch.setattr(job_search_apply, "SESSION_FILE", tmp_path / "session.json")
    monkeypatch.setattr(job_search_apply, "CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(job_search_apply, "ATS_ACCOUNTS_FILE", tmp_path / "ats_accounts.json")
    monkeypatch.setattr(
        job_search_apply, "DEEP_APPLY_QUEUE_FILE", tmp_path / "deep_apply_queue.json"
    )
    monkeypatch.setattr(job_search_apply, "DEBUG_DIR", tmp_path / "debug")
    monkeypatch.setattr(dashboard, "LOG_FILE", tmp_path / "applications.json")
    monkeypatch.setattr(dashboard, "SEARCH_LOG_FILE", tmp_path / "search_log.json")
    monkeypatch.setattr(dashboard, "DEEP_APPLY_QUEUE_FILE", tmp_path / "deep_apply_queue.json")
    monkeypatch.setattr(dashboard, "INTERVIEWS_FILE", tmp_path / "interviews.json")
    monkeypatch.setattr(dashboard, "DATA_DIR", tmp_path)
    return tmp_path
