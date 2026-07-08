"""Tests for the Flask dashboard (dashboard.py): pure helpers and HTTP routes."""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard  # noqa: E402


def _write_json(path, data):
    path.write_text(json.dumps(data))


def _app_entry(**overrides):
    entry = {
        "job_id": "li_job1",
        "title": "Senior DevOps Engineer",
        "company": "TechCo",
        "location": "Remote",
        "url": "https://www.linkedin.com/jobs/view/1",
        "status": "submitted",
        "match_score": 0.85,
        "timestamp": "2026-06-01 10:00:00",
        "ats_platform": "greenhouse",
        "cost_usd": 0.05,
    }
    entry.update(overrides)
    return entry


def _queue_entry(**overrides):
    entry = {
        "job_id": "li_q1",
        "title": "Senior SRE",
        "company": "Acme",
        "url": "https://boards.example.com/apply/1",
        "match_score": 0.92,
        "failure_reason": "form_stuck",
        "status": "pending",
        "queue": "q2",
        "q2_attempts": 0,
        "queued_at": "2026-06-01 12:00:00",
        "pre_computed": {"field_answers": [], "cover_letter_path": ""},
        "decision_log": [],
    }
    entry.update(overrides)
    return entry


PROFILE_DATA = {
    "profile": {
        "personal": {
            "full_name": "Test User",
            "email": "test@example.com",
            "phone": "+1-555-123-4567",
            "location": {"city": "Indianapolis", "state": "IN", "zip_code": "46201"},
            "linkedin_url": "https://linkedin.com/in/testuser",
            "github_url": "https://github.com/testuser",
        },
        "work_authorization": {
            "authorized_to_work_us": True,
            "requires_visa_sponsorship": False,
        },
        "experience": {
            "years_total": 9,
            "current_title": "Senior Site Reliability Engineer",
            "current_employer": "Acme Corp",
            "specializations": ["SRE"],
        },
        "skills": {
            "programming_languages": ["Python"],
            "frameworks": [],
            "tools": ["Docker", "Kubernetes"],
        },
        "documents": {"resume_path": "", "cover_letter_template_path": None},
        "application_settings": {"min_match_score": 0.75},
        "screening_answers": {"salary": "150000"},
    }
}


@pytest.fixture
def client(data_dir):
    dashboard.app.config["TESTING"] = True
    with dashboard.app.test_client() as c:
        yield c


class TestParseTs:
    def test_valid_timestamp(self):
        assert dashboard._parse_ts("2026-06-01 10:30:00") == datetime(2026, 6, 1, 10, 30, 0)

    def test_garbage_returns_none(self):
        assert dashboard._parse_ts("not a timestamp") is None

    def test_empty_string_returns_none(self):
        assert dashboard._parse_ts("") is None


class TestLoadJson:
    def test_missing_file_returns_empty_list(self, tmp_path):
        assert dashboard._load_json(tmp_path / "nope.json") == []

    def test_corrupt_json_returns_empty_list(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not valid json")
        assert dashboard._load_json(path) == []

    def test_valid_json_returned(self, tmp_path):
        path = tmp_path / "ok.json"
        _write_json(path, [{"a": 1}])
        assert dashboard._load_json(path) == [{"a": 1}]


class TestBuildMarketStats:
    def test_empty_input(self):
        stats = dashboard._build_market_stats([])
        assert stats["roles"] == []
        assert stats["total_results"] == []
        assert stats["past_week"] == []
        assert stats["past_day"] == []
        assert stats["latest"] == {}
        assert stats["history"] == {}

    def test_latest_snapshot_wins_per_title(self):
        older = {"search_title": "SRE", "total_results": 100, "timestamp": "2026-06-01 08:00:00"}
        newer = {"search_title": "SRE", "total_results": 120, "timestamp": "2026-06-02 08:00:00"}
        stats = dashboard._build_market_stats([older, newer])
        assert stats["latest"]["SRE"] is newer
        assert stats["total_results"] == [120]

    def test_older_snapshot_does_not_replace_newer(self):
        newer = {"search_title": "SRE", "total_results": 120, "timestamp": "2026-06-02 08:00:00"}
        older = {"search_title": "SRE", "total_results": 100, "timestamp": "2026-06-01 08:00:00"}
        stats = dashboard._build_market_stats([newer, older])
        assert stats["latest"]["SRE"] is newer

    def test_unparseable_timestamp_never_promoted(self):
        # Suspicion: an entry with a broken timestamp can never become the
        # "latest" snapshot even if it is actually the newest data point.
        first = {"search_title": "SRE", "total_results": 100, "timestamp": "2026-06-01 08:00:00"}
        broken = {"search_title": "SRE", "total_results": 999, "timestamp": "yesterday-ish"}
        stats = dashboard._build_market_stats([first, broken])
        assert stats["latest"]["SRE"] is first
        # But it is still kept in the history timeline
        assert len(stats["history"]["SRE"]) == 2

    def test_unparseable_timestamp_first_is_replaced_by_parseable(self):
        # A broken-timestamp entry sorts as oldest: even when it arrives
        # first, any parseable entry takes over as latest, without crashing.
        broken = {"search_title": "SRE", "total_results": 999, "timestamp": "yesterday-ish"}
        parseable = {
            "search_title": "SRE",
            "total_results": 100,
            "timestamp": "2026-06-01 08:00:00",
        }
        stats = dashboard._build_market_stats([broken, parseable])
        assert stats["latest"]["SRE"] is parseable
        assert len(stats["history"]["SRE"]) == 2

    def test_roles_sorted_by_total_results_desc(self):
        entries = [
            {"search_title": "SRE", "total_results": 120, "timestamp": "2026-06-01 08:00:00"},
            {"search_title": "DevOps", "total_results": 200, "timestamp": "2026-06-01 08:00:00"},
            {"search_title": "Platform", "total_results": 50, "timestamp": "2026-06-01 08:00:00"},
        ]
        stats = dashboard._build_market_stats(entries)
        assert stats["roles"] == ["DevOps", "SRE", "Platform"]
        assert stats["total_results"] == [200, 120, 50]

    def test_none_counts_coerced_to_zero(self):
        entry = {
            "search_title": "SRE",
            "total_results": None,
            "past_week_results": None,
            "past_day_results": None,
            "timestamp": "2026-06-01 08:00:00",
        }
        stats = dashboard._build_market_stats([entry])
        assert stats["total_results"] == [0]
        assert stats["past_week"] == [0]
        assert stats["past_day"] == [0]

    def test_missing_title_grouped_as_unknown(self):
        stats = dashboard._build_market_stats(
            [{"total_results": 5, "timestamp": "2026-06-01 08:00:00"}]
        )
        assert stats["roles"] == ["Unknown"]


class TestSnapshotStaleness:
    STALE_TEXT = b"Market snapshot data is stale since"
    EMPTY_TEXT = b"No market snapshot data recorded"

    def _snapshot(self, ts):
        return {"search_title": "SRE", "total_results": 100, "timestamp": ts}

    def test_check_fresh_returns_none(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        assert dashboard._check_snapshot_staleness([self._snapshot(now)]) is None

    def test_check_stale_returns_last_timestamp(self):
        old = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
        info = dashboard._check_snapshot_staleness([self._snapshot(old)])
        assert info == {"last_snapshot": old}

    def test_check_empty_log_is_stale_without_timestamp(self):
        assert dashboard._check_snapshot_staleness([]) == {"last_snapshot": None}

    def test_check_uses_newest_entry(self):
        old = (datetime.now() - timedelta(hours=100)).strftime("%Y-%m-%d %H:%M:%S")
        fresh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        assert (
            dashboard._check_snapshot_staleness([self._snapshot(old), self._snapshot(fresh)])
            is None
        )

    def test_check_unparseable_timestamps_treated_as_empty(self):
        info = dashboard._check_snapshot_staleness([self._snapshot("garbage")])
        assert info == {"last_snapshot": None}

    def test_index_fresh_data_no_banner(self, client, data_dir):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _write_json(data_dir / "search_log.json", [self._snapshot(now)])
        resp = client.get("/")
        assert resp.status_code == 200
        assert self.STALE_TEXT not in resp.data
        assert self.EMPTY_TEXT not in resp.data

    def test_index_stale_data_renders_banner(self, client, data_dir):
        old = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
        _write_json(data_dir / "search_log.json", [self._snapshot(old)])
        resp = client.get("/")
        assert resp.status_code == 200
        assert self.STALE_TEXT in resp.data
        assert old.encode() in resp.data
        assert b"LinkedIn session may have expired" in resp.data

    def test_index_empty_log_renders_banner(self, client, data_dir):
        resp = client.get("/")
        assert resp.status_code == 200
        assert self.EMPTY_TEXT in resp.data


class TestBuildStatusCounts:
    def test_empty(self):
        assert dashboard._build_status_counts([]) == {}

    def test_categories_by_prefix(self):
        entries = [
            {"status": "submitted"},
            {"status": "submitted (external)"},
            {"status": "failed: form stuck"},
            {"status": "aborted: deal breaker"},
            {"status": "dry_run"},
            {"status": "queued"},
        ]
        counts = dashboard._build_status_counts(entries)
        assert counts == {
            "Submitted": 2,
            "Failed": 1,
            "Aborted": 1,
            "Dry Run": 1,
            "Other": 1,
        }

    def test_missing_status_counts_as_other(self):
        assert dashboard._build_status_counts([{}]) == {"Other": 1}


class TestComputeCostStats:
    def test_empty(self):
        assert dashboard._compute_cost_stats([]) == (0.0, 0.0, 0.0)

    def test_totals_and_per_status_averages(self):
        entries = [
            {"status": "submitted", "cost_usd": 0.05},
            {"status": "submitted (external)", "cost_usd": 0.07},
            {"status": "failed: stuck", "cost_usd": 0.101},
            {"status": "aborted: deal breaker", "cost_usd": 0.02},
        ]
        total, avg_submitted, avg_failed = dashboard._compute_cost_stats(entries)
        assert total == 0.24  # includes aborted cost in the total
        assert avg_submitted == 0.06
        assert avg_failed == 0.101

    def test_backfills_cost_from_ai_tokens(self):
        entry = {"status": "submitted", "ai_tokens": {"input": 100_000, "output": 50_000}}
        total, avg_submitted, _ = dashboard._compute_cost_stats([entry])
        # 100k * 2.40/1M + 50k * 12.00/1M = 0.24 + 0.60
        assert total == 0.84
        assert avg_submitted == 0.84
        # The input entry is mutated with the backfilled cost
        assert entry["cost_usd"] == 0.84

    def test_entries_without_cost_or_tokens_ignored(self):
        entries = [
            {"status": "submitted"},
            {"status": "failed: x", "ai_tokens": {"input": 0, "output": 0}},
        ]
        assert dashboard._compute_cost_stats(entries) == (0.0, 0.0, 0.0)


class TestIndexRoute:
    def test_empty_data_renders(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"LinkedIn Easy Apply" in resp.data

    def test_populated_data_renders_entries(self, client, data_dir):
        _write_json(data_dir / "applications.json", [_app_entry()])
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"TechCo" in resp.data
        assert b"Senior DevOps Engineer" in resp.data

    def test_corrupt_applications_file_still_renders(self, client, data_dir):
        (data_dir / "applications.json").write_text("{broken")
        resp = client.get("/")
        assert resp.status_code == 200

    def test_interview_merged_with_application(self, client, data_dir):
        _write_json(data_dir / "applications.json", [_app_entry(job_id="li_a1")])
        _write_json(
            data_dir / "interviews.json",
            [{"job_id": "li_a1", "added": "2026-06-05 09:00:00", "notes": "phone screen Friday"}],
        )
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"phone screen Friday" in resp.data

    def test_interview_without_application_is_dropped(self, client, data_dir):
        _write_json(
            data_dir / "interviews.json",
            [{"job_id": "li_ghost", "added": "2026-06-05 09:00:00", "notes": "orphan note"}],
        )
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"orphan note" not in resp.data

    def test_queue_sections_render(self, client, data_dir):
        queue = [
            _queue_entry(job_id="li_q2a", company="QueueCo2", queue="q2"),
            _queue_entry(job_id="li_q3a", company="QueueCo3", queue="q3"),
        ]
        _write_json(data_dir / "deep_apply_queue.json", queue)
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"QueueCo2" in resp.data
        assert b"QueueCo3" in resp.data


class TestReportRoute:
    def test_found_entry_renders(self, client, data_dir):
        _write_json(data_dir / "applications.json", [_app_entry()])
        resp = client.get("/report/li_job1")
        assert resp.status_code == 200
        assert b"Senior DevOps Engineer" in resp.data
        assert b"TechCo" in resp.data

    def test_unknown_job_404(self, client, data_dir):
        _write_json(data_dir / "applications.json", [_app_entry()])
        assert client.get("/report/li_nope").status_code == 404

    def test_cover_letter_content_included(self, client, data_dir):
        cl_path = data_dir / "cover.txt"
        cl_path.write_text("Dear Hiring Manager at TechCo")
        _write_json(
            data_dir / "applications.json",
            [_app_entry(cover_letter_path=str(cl_path))],
        )
        resp = client.get("/report/li_job1")
        assert resp.status_code == 200
        assert b"Dear Hiring Manager at TechCo" in resp.data

    def test_missing_cover_letter_file_still_renders(self, client, data_dir):
        _write_json(
            data_dir / "applications.json",
            [_app_entry(cover_letter_path=str(data_dir / "gone.txt"))],
        )
        assert client.get("/report/li_job1").status_code == 200


class TestDeepApplyRoute:
    def test_found_entry_renders_prompt(self, client, data_dir):
        _write_json(data_dir / "profile.json", PROFILE_DATA)
        entry = _queue_entry(
            pre_computed={
                "field_answers": [{"field": "Phone", "value": "555-1234"}],
                "cover_letter_path": "",
            }
        )
        _write_json(data_dir / "deep_apply_queue.json", [entry])
        resp = client.get("/deep-apply/li_q1")
        assert resp.status_code == 200
        assert b"Senior SRE at Acme" in resp.data

    def test_prompt_includes_precomputed_answers(self, client, data_dir):
        _write_json(data_dir / "profile.json", PROFILE_DATA)
        entry = _queue_entry(
            pre_computed={
                "field_answers": [{"field": "Phone", "value": "555-1234"}],
                "cover_letter_path": "",
            }
        )
        _write_json(data_dir / "deep_apply_queue.json", [entry])
        resp = client.get("/deep-apply/li_q1")
        assert b"- Phone: 555-1234" in resp.data
        # Profile screening answers are appended too
        assert b"- salary: 150000" in resp.data

    def test_unknown_job_404(self, client, data_dir):
        _write_json(data_dir / "deep_apply_queue.json", [_queue_entry()])
        assert client.get("/deep-apply/li_nope").status_code == 404


class TestDecisionLogRoute:
    def test_found_entry_renders_decisions(self, client, data_dir):
        entry = _queue_entry(
            decision_log=[
                {
                    "step": 1,
                    "timestamp": "2026-06-01 12:00:05",
                    "action": "fill",
                    "target": "input#name",
                    "value": "Test User",
                    "reasoning": "matched profile name",
                    "confidence": "high",
                }
            ]
        )
        _write_json(data_dir / "deep_apply_queue.json", [entry])
        resp = client.get("/decision-log/li_q1")
        assert resp.status_code == 200
        assert b"matched profile name" in resp.data

    def test_unknown_job_404(self, client, data_dir):
        _write_json(data_dir / "deep_apply_queue.json", [_queue_entry()])
        assert client.get("/decision-log/li_nope").status_code == 404


class TestInterviewRoutes:
    def test_add_creates_entry_and_redirects(self, client, data_dir):
        _write_json(data_dir / "applications.json", [_app_entry(job_id="li_a1")])
        resp = client.post("/interview/add/li_a1", data={"notes": "call Tuesday"})
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/"
        interviews = json.loads((data_dir / "interviews.json").read_text())
        assert len(interviews) == 1
        assert interviews[0]["job_id"] == "li_a1"
        assert interviews[0]["notes"] == "call Tuesday"
        # "added" is a parseable timestamp
        datetime.strptime(interviews[0]["added"], "%Y-%m-%d %H:%M:%S")

    def test_add_duplicate_is_noop(self, client, data_dir):
        _write_json(data_dir / "applications.json", [_app_entry(job_id="li_a1")])
        existing = [{"job_id": "li_a1", "added": "2026-06-01 09:00:00", "notes": "original"}]
        _write_json(data_dir / "interviews.json", existing)
        resp = client.post("/interview/add/li_a1", data={"notes": "new notes"})
        assert resp.status_code == 302
        interviews = json.loads((data_dir / "interviews.json").read_text())
        assert interviews == existing

    def test_add_unknown_job_404(self, client, data_dir):
        _write_json(data_dir / "applications.json", [_app_entry(job_id="li_a1")])
        assert client.post("/interview/add/li_nope", data={}).status_code == 404
        assert not (data_dir / "interviews.json").exists()

    def test_remove_deletes_entry(self, client, data_dir):
        _write_json(
            data_dir / "interviews.json",
            [
                {"job_id": "li_a1", "added": "2026-06-01 09:00:00", "notes": ""},
                {"job_id": "li_a2", "added": "2026-06-02 09:00:00", "notes": ""},
            ],
        )
        resp = client.post("/interview/remove/li_a1")
        assert resp.status_code == 302
        interviews = json.loads((data_dir / "interviews.json").read_text())
        assert [i["job_id"] for i in interviews] == ["li_a2"]

    def test_remove_without_file_does_not_create_file(self, client, data_dir):
        # Nothing matched and nothing existed: no empty interviews.json
        resp = client.post("/interview/remove/li_nope")
        assert resp.status_code == 302
        assert not (data_dir / "interviews.json").exists()

    def test_remove_unknown_job_leaves_file_untouched(self, client, data_dir):
        existing = [{"job_id": "li_a1", "added": "2026-06-01 09:00:00", "notes": ""}]
        _write_json(data_dir / "interviews.json", existing)
        before = (data_dir / "interviews.json").read_text()
        resp = client.post("/interview/remove/li_nope")
        assert resp.status_code == 302
        # The file bytes are unchanged: no rewrite happened
        assert (data_dir / "interviews.json").read_text() == before

    def test_update_changes_notes(self, client, data_dir):
        _write_json(
            data_dir / "interviews.json",
            [{"job_id": "li_a1", "added": "2026-06-01 09:00:00", "notes": "old"}],
        )
        resp = client.post("/interview/update/li_a1", data={"notes": "new"})
        assert resp.status_code == 302
        interviews = json.loads((data_dir / "interviews.json").read_text())
        assert interviews[0]["notes"] == "new"

    def test_update_unknown_job_leaves_entries_unchanged(self, client, data_dir):
        existing = [{"job_id": "li_a1", "added": "2026-06-01 09:00:00", "notes": "old"}]
        _write_json(data_dir / "interviews.json", existing)
        before = (data_dir / "interviews.json").read_text()
        resp = client.post("/interview/update/li_nope", data={"notes": "new"})
        assert resp.status_code == 302
        # The file bytes are unchanged: no rewrite happened
        assert (data_dir / "interviews.json").read_text() == before

    def test_update_without_file_does_not_create_file(self, client, data_dir):
        resp = client.post("/interview/update/li_nope", data={"notes": "new"})
        assert resp.status_code == 302
        assert not (data_dir / "interviews.json").exists()


class TestApiData:
    EXPECTED_KEYS = {
        "entries",
        "market_stats",
        "status_counts",
        "failure_categories",
        "platform_stats",
        "total_applications",
        "total_snapshots",
        "score_bins",
        "hm_sent",
        "hm_eligible",
        "total_cost",
        "avg_cost_submitted",
        "avg_cost_failed",
        "deep_pending_count",
        "deep_success_count",
        "deep_done_count",
    }

    def test_empty_data_returns_expected_keys(self, client):
        resp = client.get("/api/data")
        assert resp.status_code == 200
        payload = resp.get_json()
        assert set(payload.keys()) == self.EXPECTED_KEYS
        assert payload["entries"] == []
        assert payload["total_applications"] == 0
        assert payload["score_bins"] == [0] * 10
        assert payload["total_cost"] == 0.0

    def test_populated_stats(self, client, data_dir):
        apps = [
            _app_entry(
                job_id="a1",
                status="submitted",
                match_score=0.85,
                cost_usd=0.05,
                ats_platform="greenhouse",
                hiring_manager_messaged="sent",
                timestamp="2026-06-01 10:00:00",
            ),
            _app_entry(
                job_id="a2",
                status="failed: form validation errors",
                match_score=0.92,
                cost_usd=0.10,
                ats_platform="lever",
                failure_category="validation_error",
                timestamp="2026-06-02 11:00:00",
            ),
            _app_entry(
                job_id="a3",
                status="dry_run",
                match_score=None,
                cost_usd=None,
                ats_platform="unknown",
                timestamp="2026-06-03 12:00:00",
            ),
        ]
        _write_json(data_dir / "applications.json", apps)
        payload = client.get("/api/data").get_json()
        assert payload["status_counts"] == {"Submitted": 1, "Failed": 1, "Dry Run": 1}
        assert payload["failure_categories"] == {"validation_error": 1}
        assert payload["platform_stats"] == {
            "greenhouse": {"submitted": 1, "failed": 0},
            "lever": {"submitted": 0, "failed": 1},
        }
        assert payload["total_applications"] == 3
        assert payload["hm_sent"] == 1
        assert payload["hm_eligible"] == 1
        assert payload["total_cost"] == 0.15
        assert payload["avg_cost_submitted"] == 0.05
        assert payload["avg_cost_failed"] == 0.1

    def test_score_bins(self, client, data_dir):
        apps = [
            _app_entry(job_id="a1", match_score=1.0),
            _app_entry(job_id="a2", match_score=0.05),
            _app_entry(job_id="a3", match_score=0.85),
            _app_entry(job_id="a4", match_score=0),  # zero excluded
            _app_entry(job_id="a5", match_score=None),  # missing excluded
        ]
        _write_json(data_dir / "applications.json", apps)
        bins = client.get("/api/data").get_json()["score_bins"]
        assert bins[9] == 1  # 1.0 clamps into the top bin
        assert bins[0] == 1
        assert bins[8] == 1
        assert sum(bins) == 3

    def test_entries_sorted_newest_first(self, client, data_dir):
        apps = [
            _app_entry(job_id="old", timestamp="2026-06-01 10:00:00"),
            _app_entry(job_id="new", timestamp="2026-06-03 10:00:00"),
            _app_entry(job_id="mid", timestamp="2026-06-02 10:00:00"),
        ]
        _write_json(data_dir / "applications.json", apps)
        payload = client.get("/api/data").get_json()
        assert [e["job_id"] for e in payload["entries"]] == ["new", "mid", "old"]

    def test_deep_queue_counts(self, client, data_dir):
        queue = [
            _queue_entry(job_id="q1", status="pending"),
            _queue_entry(job_id="q2", status="done", deep_apply_status="submitted"),
            _queue_entry(job_id="q3", status="done", deep_apply_status="failed"),
            _queue_entry(job_id="q4", status="in_progress"),
        ]
        _write_json(data_dir / "deep_apply_queue.json", queue)
        payload = client.get("/api/data").get_json()
        assert payload["deep_pending_count"] == 1
        assert payload["deep_done_count"] == 2
        assert payload["deep_success_count"] == 1
