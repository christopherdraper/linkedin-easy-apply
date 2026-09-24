"""Tests for jobapply/workflow.py: application entry construction from
stats state, submit dispatch, search source dispatch, and the
auto_apply_workflow orchestration happy path."""

import sys
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jobapply.stats as stats  # noqa: E402
import jobapply.workflow as workflow  # noqa: E402
from jobapply.applog import remember_score  # noqa: E402
from jobapply.content import SCORER_FINGERPRINT  # noqa: E402
from jobapply.profile import JobSearchParams  # noqa: E402
from jobapply.workflow import (  # noqa: E402
    _build_application_entry,
    _search_source,
    _submit_one,
    auto_apply_workflow,
)

# ---------------------------------------------------------------------------
# _build_application_entry
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_stats(monkeypatch):
    """Seed the jobapply.stats module globals; monkeypatch restores after."""
    monkeypatch.setattr(stats, "_field_fills", [{"label": "Name", "value": "Test User"}])
    monkeypatch.setattr(stats, "_ai_answer_failures", [{"question": "salary?"}])
    monkeypatch.setattr(stats, "_ai_tokens_in", 100_000)
    monkeypatch.setattr(stats, "_ai_tokens_out", 10_000)
    monkeypatch.setattr(stats, "_final_ats_url", "https://boards.greenhouse.io/acme/jobs/1")
    monkeypatch.setattr(stats, "_apply_start_time", 0.0)


class TestBuildApplicationEntry:
    def test_core_fields_from_job_and_compat(self, job, compat, seeded_stats):
        entry = _build_application_entry(
            job, compat, "submitted", Path("/tmp/cl.docx"), "AI notes", None, None, None
        )
        assert entry["job_id"] == "li_abc123"
        assert entry["title"] == "Senior DevOps Engineer"
        assert entry["company"] == "TechCo"
        assert entry["url"] == job["url"]
        assert entry["status"] == "submitted"
        assert entry["match_score"] == 0.85
        assert entry["reasoning"] == "Strong match for DevOps skills"
        assert entry["deal_breakers"] == []
        assert entry["apply_type"] == "easy_apply"  # default when job has no apply_type
        assert entry["cover_letter_path"] == "/tmp/cl.docx"
        assert entry["notes"] == "AI notes"
        assert entry["hiring_manager_messaged"] is None
        assert entry["failure_category"] is None  # not a failed status

    def test_stats_snapshot_tokens_and_cost(self, job, compat, seeded_stats):
        entry = _build_application_entry(
            job, compat, "submitted", Path("/tmp/cl.docx"), "", None, None, None
        )
        assert entry["ats_url"] == "https://boards.greenhouse.io/acme/jobs/1"
        assert entry["ats_platform"] == "Greenhouse"
        assert entry["fields_filled"] == [{"label": "Name", "value": "Test User"}]
        assert entry["ai_answer_failures"] == [{"question": "salary?"}]
        assert entry["ai_tokens"] == {"input": 100_000, "output": 10_000}
        # 100k in * $2.40/M + 10k out * $12.00/M
        assert entry["cost_usd"] == 0.36
        # fields_filled is a snapshot copy, not a live reference
        stats._field_fills.append({"label": "Email", "value": "x"})
        assert entry["fields_filled"] == [{"label": "Name", "value": "Test User"}]

    def test_failure_category_and_duration(self, job, compat, seeded_stats, monkeypatch):
        monkeypatch.setattr(stats, "_apply_start_time", time.time() - 10)
        entry = _build_application_entry(
            job,
            compat,
            "failed: form stuck after 4 attempts",
            Path("/tmp/cl.docx"),
            "",
            None,
            None,
            None,
        )
        assert entry["failure_category"] == "form_stuck"
        assert 8.0 <= entry["duration_seconds"] <= 12.0

    def test_no_ats_url_falls_back_to_job_url(self, job, compat, seeded_stats, monkeypatch):
        monkeypatch.setattr(stats, "_final_ats_url", "")
        entry = _build_application_entry(
            job, compat, "submitted", Path("/tmp/cl.docx"), "", None, None, None
        )
        assert entry["ats_url"] == ""
        # Platform detection falls back to the job URL (LinkedIn -> unknown)
        assert entry["ats_platform"] == "unknown"
        # _apply_start_time of 0.0 means no duration recorded
        assert entry["duration_seconds"] is None


# ---------------------------------------------------------------------------
# _submit_one
# ---------------------------------------------------------------------------


class TestSubmitOne:
    def test_dry_run_skips_submit_functions(self, job, profile):
        job["apply_type"] = "easy_apply"
        with (
            patch("jobapply.workflow.submit_easy_apply") as mock_easy,
            patch("jobapply.workflow.submit_external_apply") as mock_ext,
        ):
            status = _submit_one(job, profile, Path("/tmp/cl.docx"), dry_run=True, proxy=None)
        assert status == "dry_run"
        mock_easy.assert_not_called()
        mock_ext.assert_not_called()

    def test_easy_apply_dispatch(self, job, profile):
        job["apply_type"] = "easy_apply"
        with (
            patch("jobapply.workflow.submit_easy_apply", return_value="submitted") as mock_easy,
            patch("jobapply.workflow.submit_external_apply") as mock_ext,
        ):
            status = _submit_one(job, profile, Path("/tmp/cl.docx"), dry_run=False, proxy=None)
        assert status == "submitted"
        mock_easy.assert_called_once_with(job, profile, proxy=None)
        mock_ext.assert_not_called()

    def test_external_dispatch(self, job, profile):
        job["apply_type"] = "external"
        with (
            patch("jobapply.workflow.submit_easy_apply") as mock_easy,
            patch(
                "jobapply.workflow.submit_external_apply",
                return_value="failed: no apply button found",
            ) as mock_ext,
        ):
            status = _submit_one(job, profile, Path("/tmp/cl.docx"), dry_run=False, proxy=None)
        assert status == "failed: no apply button found"
        mock_ext.assert_called_once_with(
            job,
            profile,
            cover_letter_path="/tmp/cl.docx",
            proxy=None,
            dry_run=False,
            dedupe_ats=True,
        )
        mock_easy.assert_not_called()


# ---------------------------------------------------------------------------
# _search_source
# ---------------------------------------------------------------------------


class TestSearchSource:
    def test_dispatches_named_sources(self):
        params = JobSearchParams(title="sre")
        with (
            patch("jobapply.workflow.search_remoteok", return_value=[{"id": "rok_1"}]) as m_rok,
            patch("jobapply.workflow.search_hn_whos_hiring", return_value=[{"id": "hn_1"}]) as m_hn,
            patch("jobapply.workflow.search_biotech", return_value=[{"id": "bio_1"}]) as m_bio,
            patch("jobapply.workflow.search_linkedin") as m_li,
        ):
            assert _search_source("remoteok", params, None) == [{"id": "rok_1"}]
            assert _search_source("hn", params, None) == [{"id": "hn_1"}]
            assert _search_source("biotech", params, None) == [{"id": "bio_1"}]
        m_rok.assert_called_once_with(params)
        m_hn.assert_called_once_with(params)
        m_bio.assert_called_once_with(params)
        m_li.assert_not_called()

    def test_defaults_to_linkedin_with_proxy(self):
        params = JobSearchParams(title="sre")
        with patch("jobapply.workflow.search_linkedin", return_value=[{"id": "li_1"}]) as m_li:
            assert _search_source("linkedin", params, "socks5://localhost:1080") == [{"id": "li_1"}]
        m_li.assert_called_once_with(params, proxy="socks5://localhost:1080", need_description=None)


class TestNeedsDescription:
    """Only jobs the batch will score or apply to need their LinkedIn page loaded.

    Regression driver (2026-09-23): every search re-loaded all 25 job pages,
    including jobs already applied to or already rejected, across repeated
    overlapping-title runs. LinkedIn revoked the session within the hour.
    """

    def _cache(self, job_id, score, deal_breakers=()):
        from jobapply.workflow import _needs_description  # noqa: F401  (import check)

        cache = {}
        remember_score(
            cache,
            job_id,
            {"match_score": score, "reasoning": "", "deal_breakers": list(deal_breakers)},
            True,
            SCORER_FINGERPRINT,
        )
        return cache

    def test_already_applied_needs_no_page(self):
        from jobapply.workflow import _needs_description

        job = {"id": "li_1", "url": "https://www.linkedin.com/jobs/view/1/?trk=x"}
        applied = {"https://www.linkedin.com/jobs/view/1/"}
        assert _needs_description(job, applied, {}, 0.6) is False

    def test_cached_below_bar_needs_no_page(self):
        from jobapply.workflow import _needs_description

        job = {"id": "li_2", "url": "https://www.linkedin.com/jobs/view/2/"}
        assert _needs_description(job, set(), self._cache("li_2", 0.35), 0.6) is False

    def test_cached_with_deal_breaker_needs_no_page(self):
        from jobapply.workflow import _needs_description

        job = {"id": "li_3", "url": "https://www.linkedin.com/jobs/view/3/"}
        cache = self._cache("li_3", 0.9, ["relocation_required"])
        assert _needs_description(job, set(), cache, 0.6) is False

    def test_cached_above_bar_still_needs_page_for_cover_letter(self):
        from jobapply.workflow import _needs_description

        job = {"id": "li_4", "url": "https://www.linkedin.com/jobs/view/4/"}
        assert _needs_description(job, set(), self._cache("li_4", 0.62), 0.6) is True

    def test_unscored_job_needs_page(self):
        from jobapply.workflow import _needs_description

        job = {"id": "li_5", "url": "https://www.linkedin.com/jobs/view/5/"}
        assert _needs_description(job, set(), {}, 0.6) is True


# ---------------------------------------------------------------------------
# auto_apply_workflow
# ---------------------------------------------------------------------------


def _workflow_seams(stack, jobs, compat, cl_path, submit_status="submitted"):
    """Patch every collaborator auto_apply_workflow reaches, in its namespace."""
    mocks = {
        "search": stack.enter_context(patch("jobapply.workflow._search_source", return_value=jobs)),
        "score": stack.enter_context(patch("jobapply.workflow.ai_score_job", return_value=compat)),
        "cover": stack.enter_context(
            patch("jobapply.workflow.ai_generate_cover_letter", return_value="Dear team,")
        ),
        "save_cl": stack.enter_context(
            patch("jobapply.workflow._save_cover_letter_docx", return_value=cl_path)
        ),
        "notes": stack.enter_context(
            patch("jobapply.workflow.ai_build_notes", return_value="notes")
        ),
        "submit": stack.enter_context(
            patch("jobapply.workflow._submit_one", return_value=submit_status)
        ),
        "load_log": stack.enter_context(patch("jobapply.workflow.load_log", return_value=[])),
        "already": stack.enter_context(
            patch("jobapply.workflow.already_applied", return_value=set())
        ),
        "save_log": stack.enter_context(patch("jobapply.workflow.save_log")),
        "load_score_cache": stack.enter_context(
            patch("jobapply.workflow.load_score_cache", return_value={})
        ),
        "save_score_cache": stack.enter_context(patch("jobapply.workflow.save_score_cache")),
        "delay": stack.enter_context(patch("jobapply.workflow._human_delay")),
        "reset": stack.enter_context(patch("jobapply.workflow.stats.reset_run_stats")),
    }
    return mocks


class TestAutoApplyWorkflow:
    def test_happy_path_produces_one_application(self, data_dir, monkeypatch, job, compat, profile):
        monkeypatch.setattr(workflow, "COVER_LETTER_DIR", data_dir / "cover-letters")
        cl_path = data_dir / "cover-letters" / "cl_li_abc123.docx"
        params = JobSearchParams(title="devops engineer")

        with ExitStack() as stack:
            mocks = _workflow_seams(stack, [dict(job)], compat, cl_path)
            result = auto_apply_workflow(params, profile, max_applications=5, min_match_score=0.5)

        assert result["jobs_found"] == 1
        assert result["total"] == 1
        apps = result["applications"]
        assert len(apps) == 1
        assert apps[0]["job_id"] == "li_abc123"
        assert apps[0]["status"] == "submitted"
        assert apps[0]["match_score"] == 0.85
        mocks["reset"].assert_called_once()
        mocks["submit"].assert_called_once()
        mocks["save_log"].assert_called_once_with(apps)

    def test_low_score_job_is_skipped(self, data_dir, monkeypatch, job, profile):
        monkeypatch.setattr(workflow, "COVER_LETTER_DIR", data_dir / "cover-letters")
        low_compat = {"match_score": 0.2, "reasoning": "weak overlap", "deal_breakers": []}
        params = JobSearchParams(title="devops engineer")

        with ExitStack() as stack:
            mocks = _workflow_seams(stack, [dict(job)], low_compat, data_dir / "cl.docx")
            result = auto_apply_workflow(params, profile, max_applications=5, min_match_score=0.5)

        assert result == {"applications": [], "total": 0, "jobs_found": 1}
        mocks["submit"].assert_not_called()
        mocks["cover"].assert_not_called()
        mocks["save_log"].assert_called_once_with([])

    def test_rejected_job_is_not_rescored_on_a_later_run(self, data_dir, monkeypatch, job, profile):
        """The waste this cache exists to stop: the same posting surfacing under
        several overlapping title searches and being re-scored every time."""
        monkeypatch.setattr(workflow, "COVER_LETTER_DIR", data_dir / "cover-letters")
        low_compat = {"match_score": 0.2, "reasoning": "weak overlap", "deal_breakers": []}
        params = JobSearchParams(title="devops engineer")
        shared_cache = {}

        for _ in range(2):
            with ExitStack() as stack:
                mocks = _workflow_seams(stack, [dict(job)], low_compat, data_dir / "cl.docx")
                mocks["load_score_cache"].return_value = shared_cache
                auto_apply_workflow(params, profile, max_applications=5, min_match_score=0.5)
                scored_calls = mocks["score"].call_count

        # Second run reused the cached verdict instead of calling the scorer.
        assert scored_calls == 0
        assert shared_cache[job["id"]]["match_score"] == 0.2

    def test_score_cache_not_written_when_nothing_new_scored(
        self, data_dir, monkeypatch, job, profile
    ):
        monkeypatch.setattr(workflow, "COVER_LETTER_DIR", data_dir / "cover-letters")
        low_compat = {"match_score": 0.2, "reasoning": "weak", "deal_breakers": []}
        params = JobSearchParams(title="devops engineer")
        primed = {}
        remember_score(primed, job["id"], low_compat, True, SCORER_FINGERPRINT)

        with ExitStack() as stack:
            mocks = _workflow_seams(stack, [dict(job)], low_compat, data_dir / "cl.docx")
            mocks["load_score_cache"].return_value = primed
            auto_apply_workflow(params, profile, max_applications=5, min_match_score=0.5)

        mocks["score"].assert_not_called()
        mocks["save_score_cache"].assert_not_called()

    def test_passes_a_need_description_filter_to_the_search(self, data_dir, profile):
        params = JobSearchParams(title="devops engineer")
        with patch("jobapply.workflow._search_source", return_value=[]) as m_search:
            auto_apply_workflow(params, profile)
        need = m_search.call_args.kwargs.get("need_description")
        assert callable(need)

    def test_search_failure_returns_empty_result(self, data_dir, profile):
        params = JobSearchParams(title="devops engineer")
        with patch(
            "jobapply.workflow._search_source",
            side_effect=RuntimeError("No results found — LinkedIn may have changed layout."),
        ):
            result = auto_apply_workflow(params, profile)
        assert result == {"applications": [], "total": 0, "jobs_found": 0}

    def test_session_expired_propagates_so_the_batch_stops(self, data_dir, profile):
        """cli._run_batch stops the whole batch on "session expired", but only if
        the error reaches it. Swallowing it here let a batch run ~an hour logged
        out on 2026-09-23, searching every remaining title for nothing."""
        params = JobSearchParams(title="devops engineer")
        with patch(
            "jobapply.workflow._search_source",
            side_effect=RuntimeError("LinkedIn session expired: no li_at cookie"),
        ):
            with pytest.raises(RuntimeError, match="session expired"):
                auto_apply_workflow(params, profile)
