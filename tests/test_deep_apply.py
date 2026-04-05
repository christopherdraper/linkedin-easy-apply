"""Tests for deep-apply queue system."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_search_apply import (  # noqa: E402
    _deep_apply_eligible,
    _deep_apply_queue_path,
    _load_deep_apply_queue,
    _queue_for_deep_apply,
    _save_deep_apply_queue,
)


class TestDeepApplyEligible:
    def test_eligible_high_score_form_stuck(self):
        entry = {
            "match_score": 0.94,
            "status": "failed: form stuck",
            "failure_category": "form_stuck",
        }
        assert _deep_apply_eligible(entry, set()) is True

    def test_eligible_score_exactly_0_9(self):
        entry = {
            "match_score": 0.90,
            "status": "failed: validation",
            "failure_category": "validation_error",
        }
        assert _deep_apply_eligible(entry, set()) is True

    def test_ineligible_low_score(self):
        entry = {
            "match_score": 0.85,
            "status": "failed: form stuck",
            "failure_category": "form_stuck",
        }
        assert _deep_apply_eligible(entry, set()) is False

    def test_ineligible_timeout(self):
        entry = {"match_score": 0.95, "status": "failed: timeout", "failure_category": "timeout"}
        assert _deep_apply_eligible(entry, set()) is False

    def test_ineligible_aborted(self):
        entry = {"match_score": 0.95, "status": "aborted: injection", "failure_category": None}
        assert _deep_apply_eligible(entry, set()) is False

    def test_ineligible_other_category(self):
        entry = {"match_score": 0.95, "status": "failed: unknown", "failure_category": "other"}
        assert _deep_apply_eligible(entry, set()) is False

    def test_ineligible_already_queued(self):
        entry = {
            "match_score": 0.94,
            "status": "failed: form stuck",
            "failure_category": "form_stuck",
            "job_id": "li_abc",
        }
        assert _deep_apply_eligible(entry, {"li_abc"}) is False

    def test_ineligible_submitted(self):
        entry = {"match_score": 0.94, "status": "submitted", "failure_category": None}
        assert _deep_apply_eligible(entry, set()) is False

    def test_eligible_captcha(self):
        entry = {"match_score": 0.92, "status": "failed: captcha", "failure_category": "captcha"}
        assert _deep_apply_eligible(entry, set()) is True

    def test_eligible_login_wall(self):
        entry = {
            "match_score": 0.91,
            "status": "failed: login wall",
            "failure_category": "login_wall",
        }
        assert _deep_apply_eligible(entry, set()) is True

    def test_eligible_max_steps(self):
        entry = {
            "match_score": 0.90,
            "status": "failed: max steps",
            "failure_category": "max_steps",
        }
        assert _deep_apply_eligible(entry, set()) is True

    def test_eligible_no_apply_button(self):
        entry = {
            "match_score": 0.93,
            "status": "failed: no apply button",
            "failure_category": "no_apply_button",
        }
        assert _deep_apply_eligible(entry, set()) is True

    def test_eligible_modal_lost(self):
        entry = {
            "match_score": 0.95,
            "status": "failed: modal lost",
            "failure_category": "modal_lost",
        }
        assert _deep_apply_eligible(entry, set()) is True


class TestDeepApplyQueuePath:
    def test_returns_path(self):
        result = _deep_apply_queue_path()
        assert isinstance(result, Path)
        assert result.name == "deep_apply_queue.json"


class TestDeepApplyQueueStorage:
    def test_load_empty(self, tmp_path):
        with patch("job_search_apply.DEEP_APPLY_QUEUE_FILE", tmp_path / "queue.json"):
            assert _load_deep_apply_queue() == []

    def test_save_and_load(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        entry = {"job_id": "li_abc", "status": "pending"}
        with patch("job_search_apply.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("job_search_apply.DATA_DIR", tmp_path):
                _save_deep_apply_queue([entry])
            result = _load_deep_apply_queue()
        assert len(result) == 1
        assert result[0]["job_id"] == "li_abc"

    def test_load_corrupt_file(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        queue_file.write_text("not json")
        with patch("job_search_apply.DEEP_APPLY_QUEUE_FILE", queue_file):
            assert _load_deep_apply_queue() == []


class TestQueueForDeepApply:
    def test_queues_eligible_entry(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        app_entry = {
            "job_id": "li_abc123",
            "title": "Senior SRE",
            "company": "Acme Corp",
            "url": "https://linkedin.com/jobs/view/123",
            "match_score": 0.94,
            "status": "failed: external form stuck (step 6/20)",
            "failure_category": "form_stuck",
            "cover_letter_path": "/tmp/cl.txt",
            "reasoning": "Strong match on SRE, Kubernetes, AWS",
        }
        with patch("job_search_apply.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("job_search_apply.DATA_DIR", tmp_path):
                with patch(
                    "job_search_apply._field_fills",
                    [
                        {"field": "city*", "value": "Indianapolis", "source": "contact"},
                    ],
                ):
                    _queue_for_deep_apply(app_entry)

        queue = json.loads(queue_file.read_text())
        assert len(queue) == 1
        q = queue[0]
        assert q["job_id"] == "li_abc123"
        assert q["status"] == "pending"
        assert q["match_score"] == 0.94
        assert q["pre_computed"]["cover_letter_path"] == "/tmp/cl.txt"
        assert len(q["pre_computed"]["field_answers"]) == 1
        assert q["pre_computed"]["field_answers"][0]["field"] == "city*"

    def test_appends_to_existing_queue(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        queue_file.write_text(json.dumps([{"job_id": "existing", "status": "pending"}]))
        app_entry = {
            "job_id": "li_new",
            "title": "DevOps",
            "company": "Corp",
            "url": "https://example.com",
            "match_score": 0.91,
            "status": "failed: captcha",
            "failure_category": "captcha",
            "cover_letter_path": "",
            "reasoning": "",
        }
        with patch("job_search_apply.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("job_search_apply.DATA_DIR", tmp_path):
                with patch("job_search_apply._field_fills", []):
                    _queue_for_deep_apply(app_entry)

        queue = json.loads(queue_file.read_text())
        assert len(queue) == 2
        assert queue[0]["job_id"] == "existing"
        assert queue[1]["job_id"] == "li_new"
