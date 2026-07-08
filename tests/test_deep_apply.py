"""Tests for deep-apply queue system."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_search_apply import (  # noqa: E402
    ApplicantProfile,
    _deep_apply_eligible,
    _escalate_to_q3,
    _generate_deep_apply_prompt,
    _load_deep_apply_queue,
    _mark_deep_apply_done,
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

    def test_eligible_score_exactly_0_7(self):
        entry = {
            "match_score": 0.70,
            "status": "failed: validation",
            "failure_category": "validation_error",
        }
        assert _deep_apply_eligible(entry, set()) is True

    def test_ineligible_low_score(self):
        entry = {
            "match_score": 0.65,
            "status": "failed: form stuck",
            "failure_category": "form_stuck",
        }
        assert _deep_apply_eligible(entry, set()) is False

    def test_eligible_medium_score(self):
        """Score >= 0.7 is now eligible for Q2 retry."""
        entry = {
            "match_score": 0.75,
            "status": "failed: form stuck",
            "failure_category": "form_stuck",
        }
        assert _deep_apply_eligible(entry, set()) is True

    def test_eligible_timeout(self):
        """Timeout is now an eligible category."""
        entry = {"match_score": 0.95, "status": "failed: timeout", "failure_category": "timeout"}
        assert _deep_apply_eligible(entry, set()) is True

    def test_eligible_unknown_error(self):
        entry = {
            "match_score": 0.80,
            "status": "failed: unknown",
            "failure_category": "unknown_error",
        }
        assert _deep_apply_eligible(entry, set()) is True

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


class TestDeepApplyQueueStorage:
    def test_load_empty(self, tmp_path):
        with patch("jobapply.queue.DEEP_APPLY_QUEUE_FILE", tmp_path / "queue.json"):
            assert _load_deep_apply_queue() == []

    def test_save_and_load(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        entry = {"job_id": "li_abc", "status": "pending"}
        with patch("jobapply.queue.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("jobapply.queue.DATA_DIR", tmp_path):
                _save_deep_apply_queue([entry])
            result = _load_deep_apply_queue()
        assert len(result) == 1
        assert result[0]["job_id"] == "li_abc"

    def test_load_corrupt_file(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        queue_file.write_text("not json")
        with patch("jobapply.queue.DEEP_APPLY_QUEUE_FILE", queue_file):
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
        with patch("jobapply.queue.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("jobapply.queue.DATA_DIR", tmp_path):
                with patch(
                    "jobapply.stats._field_fills",
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
        with patch("jobapply.queue.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("jobapply.queue.DATA_DIR", tmp_path):
                with patch("jobapply.stats._field_fills", []):
                    _queue_for_deep_apply(app_entry)

        queue = json.loads(queue_file.read_text())
        assert len(queue) == 2
        assert queue[0]["job_id"] == "existing"
        assert queue[1]["job_id"] == "li_new"


class TestGenerateDeepApplyPrompt:
    def _make_profile(self):
        return ApplicantProfile(
            full_name="Chris Draper",
            email="chris@example.com",
            phone="555-1234",
            resume_path="/home/user/resume.pdf",
            current_title="Senior SRE",
            current_employer="Acme Inc",
            years_experience=12,
            screening_answers={"salary": "150000", "city": "Indianapolis"},
        )

    def test_prompt_contains_job_details(self):
        profile = self._make_profile()
        queue_entry = {
            "job_id": "li_abc",
            "title": "Staff SRE",
            "company": "BigCo",
            "url": "https://example.com/apply",
            "match_score": 0.94,
            "pre_computed": {
                "cover_letter_path": "/tmp/cl.txt",
                "field_answers": [
                    {"field": "city*", "value": "Indianapolis", "source": "contact"},
                ],
                "scoring_reasoning": "Great match",
            },
        }
        prompt = _generate_deep_apply_prompt(queue_entry, profile)
        assert "Staff SRE" in prompt
        assert "BigCo" in prompt
        assert "https://example.com/apply" in prompt
        assert "94%" in prompt

    def test_prompt_contains_field_answers(self):
        profile = self._make_profile()
        queue_entry = {
            "job_id": "li_abc",
            "title": "SRE",
            "company": "Co",
            "url": "https://example.com",
            "match_score": 0.90,
            "pre_computed": {
                "cover_letter_path": "",
                "field_answers": [
                    {"field": "state*", "value": "Indiana", "source": "extjs_boxselect"},
                    {"field": "city*", "value": "Indianapolis", "source": "contact"},
                ],
                "scoring_reasoning": "",
            },
        }
        prompt = _generate_deep_apply_prompt(queue_entry, profile)
        assert "state*" in prompt
        assert "Indiana" in prompt
        assert "city*" in prompt
        assert "Indianapolis" in prompt

    def test_prompt_contains_screening_answers(self):
        profile = self._make_profile()
        queue_entry = {
            "job_id": "li_abc",
            "title": "SRE",
            "company": "Co",
            "url": "https://example.com",
            "match_score": 0.90,
            "pre_computed": {
                "cover_letter_path": "",
                "field_answers": [],
                "scoring_reasoning": "",
            },
        }
        prompt = _generate_deep_apply_prompt(queue_entry, profile)
        assert "salary" in prompt
        assert "150000" in prompt

    def test_prompt_contains_profile_facts(self):
        profile = self._make_profile()
        queue_entry = {
            "job_id": "li_abc",
            "title": "SRE",
            "company": "Co",
            "url": "https://example.com",
            "match_score": 0.90,
            "pre_computed": {
                "cover_letter_path": "",
                "field_answers": [],
                "scoring_reasoning": "",
            },
        }
        prompt = _generate_deep_apply_prompt(queue_entry, profile)
        assert "Acme Inc" in prompt
        assert "Senior SRE" in prompt
        assert "12" in prompt
        assert "~/Downloads/resume.pdf" in prompt


class TestMarkDeepApplyDone:
    def test_marks_submitted(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        queue = [
            {
                "job_id": "li_abc",
                "status": "pending",
                "deep_apply_status": None,
                "deep_apply_timestamp": None,
                "deep_apply_cost": None,
            },
        ]
        queue_file.write_text(json.dumps(queue))

        with patch("jobapply.queue.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("jobapply.queue.DATA_DIR", tmp_path):
                result = _mark_deep_apply_done("li_abc", "submitted", None)

        assert result is True
        updated = json.loads(queue_file.read_text())
        assert updated[0]["status"] == "done"
        assert updated[0]["deep_apply_status"] == "submitted"
        assert updated[0]["deep_apply_timestamp"] is not None

    def test_marks_failed_with_reason(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        queue = [
            {
                "job_id": "li_abc",
                "status": "pending",
                "deep_apply_status": None,
                "deep_apply_timestamp": None,
                "deep_apply_cost": None,
            },
        ]
        queue_file.write_text(json.dumps(queue))

        with patch("jobapply.queue.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("jobapply.queue.DATA_DIR", tmp_path):
                result = _mark_deep_apply_done("li_abc", "failed", "site was down")

        assert result is True
        updated = json.loads(queue_file.read_text())
        assert updated[0]["deep_apply_status"] == "failed"

    def test_returns_false_if_not_found(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        queue_file.write_text("[]")

        with patch("jobapply.queue.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("jobapply.queue.DATA_DIR", tmp_path):
                result = _mark_deep_apply_done("li_nonexist", "submitted", None)

        assert result is False


class TestDeepApplyIntegration:
    def test_full_queue_prompt_done_flow(self, tmp_path):
        """End-to-end: queue an entry, generate prompt, mark done."""
        queue_file = tmp_path / "queue.json"
        log_file = tmp_path / "applications.json"

        app_entry = {
            "job_id": "li_integ",
            "title": "Platform Engineer",
            "company": "TestCo",
            "url": "https://example.com/apply/123",
            "match_score": 0.92,
            "status": "failed: external form stuck (step 4/15)",
            "failure_category": "form_stuck",
            "cover_letter_path": "/tmp/cl_integ.txt",
            "reasoning": "Strong K8s and AWS match",
        }

        # Write a mock application log
        log_file.write_text(json.dumps([app_entry]))

        profile = ApplicantProfile(
            full_name="Test User",
            email="test@example.com",
            phone="555-0000",
            resume_path="/tmp/resume.pdf",
            current_title="SRE",
            current_employer="CurrentCo",
            years_experience=10,
            screening_answers={"salary": "150000"},
        )

        with patch("jobapply.queue.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("jobapply.queue.DATA_DIR", tmp_path):
                with patch(
                    "jobapply.stats._field_fills",
                    [
                        {"field": "city", "value": "Indy", "source": "contact"},
                    ],
                ):
                    # Step 1: Check eligibility
                    assert _deep_apply_eligible(app_entry, set()) is True

                    # Step 2: Queue it
                    _queue_for_deep_apply(app_entry)
                    queue = _load_deep_apply_queue()
                    assert len(queue) == 1
                    assert queue[0]["status"] == "pending"

                    # Step 3: Generate prompt
                    prompt = _generate_deep_apply_prompt(queue[0], profile)
                    assert "Platform Engineer" in prompt
                    assert "TestCo" in prompt
                    assert "city" in prompt
                    assert "~/Downloads/resume.pdf" in prompt

                    # Step 4: Mark done
                    with (
                        patch("jobapply.queue.LOG_FILE", log_file),
                        patch("jobapply.applog.LOG_FILE", log_file),
                    ):
                        ok = _mark_deep_apply_done("li_integ", "submitted", None)
                    assert ok is True

                    # Verify queue updated
                    updated_queue = _load_deep_apply_queue()
                    assert updated_queue[0]["status"] == "done"
                    assert updated_queue[0]["deep_apply_status"] == "submitted"

                    # Verify app log updated
                    updated_log = json.loads(log_file.read_text())
                    assert updated_log[0]["deep_apply_status"] == "submitted"


class TestQueueSchemaExtensions:
    """Tests for new Q2/Q3 queue fields."""

    def test_queue_entry_has_q2_fields(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        app_entry = {
            "job_id": "li_schema",
            "title": "SRE",
            "company": "Co",
            "url": "https://example.com",
            "match_score": 0.80,
            "status": "failed: form stuck",
            "failure_category": "form_stuck",
            "cover_letter_path": "",
            "reasoning": "",
        }
        with patch("jobapply.queue.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("jobapply.queue.DATA_DIR", tmp_path):
                with patch("jobapply.stats._field_fills", []):
                    _queue_for_deep_apply(app_entry)

        queue = json.loads(queue_file.read_text())
        q = queue[0]
        assert q["queue"] == "q2"
        assert q["q2_attempts"] == 0
        assert q["decision_log"] == []

    def test_queue_entry_with_q3_tier(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        app_entry = {
            "job_id": "li_q3",
            "title": "SRE",
            "company": "Co",
            "url": "https://example.com",
            "match_score": 0.90,
            "status": "failed: captcha",
            "failure_category": "captcha",
            "cover_letter_path": "",
            "reasoning": "",
        }
        with patch("jobapply.queue.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("jobapply.queue.DATA_DIR", tmp_path):
                with patch("jobapply.stats._field_fills", []):
                    _queue_for_deep_apply(app_entry, queue_tier="q3")

        queue = json.loads(queue_file.read_text())
        assert queue[0]["queue"] == "q3"

    def test_backward_compat_missing_queue_field(self):
        """Entries without 'queue' field should be treated as q2."""
        old_entry = {"job_id": "old", "status": "pending"}
        assert old_entry.get("queue", "q2") == "q2"


class TestEscalateToQ3:
    def test_escalates_q2_to_q3(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        queue = [
            {
                "job_id": "li_esc",
                "queue": "q2",
                "status": "in_progress",
                "q2_attempts": 2,
                "title": "SRE",
                "company": "Co",
            }
        ]
        queue_file.write_text(json.dumps(queue))

        with patch("jobapply.queue.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("jobapply.queue.DATA_DIR", tmp_path):
                result = _escalate_to_q3("li_esc", "stuck on captcha page")

        assert result is True
        updated = json.loads(queue_file.read_text())
        assert updated[0]["queue"] == "q3"
        assert updated[0]["status"] == "pending"
        assert updated[0]["escalation_reason"] == "stuck on captcha page"

    def test_escalate_not_found(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        queue_file.write_text("[]")

        with patch("jobapply.queue.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("jobapply.queue.DATA_DIR", tmp_path):
                result = _escalate_to_q3("li_nonexist", "reason")

        assert result is False


class TestMarkDoneWithDecisionLog:
    def test_persists_decision_log(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        queue = [
            {
                "job_id": "li_log",
                "status": "pending",
                "deep_apply_status": None,
                "deep_apply_timestamp": None,
                "deep_apply_cost": None,
            }
        ]
        queue_file.write_text(json.dumps(queue))

        decision_log = [
            {
                "step": 1,
                "action": "fill_field",
                "target": "First Name",
                "value": "Matt",
                "reasoning": "Profile match",
                "confidence": "high",
            }
        ]

        with patch("jobapply.queue.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("jobapply.queue.DATA_DIR", tmp_path):
                result = _mark_deep_apply_done(
                    "li_log", "submitted", None, decision_log=decision_log
                )

        assert result is True
        updated = json.loads(queue_file.read_text())
        assert updated[0]["decision_log"] == decision_log
        assert len(updated[0]["decision_log"]) == 1

    def test_no_decision_log_leaves_field_unchanged(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        queue = [
            {
                "job_id": "li_nolog",
                "status": "pending",
                "deep_apply_status": None,
                "deep_apply_timestamp": None,
                "deep_apply_cost": None,
            }
        ]
        queue_file.write_text(json.dumps(queue))

        with patch("jobapply.queue.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("jobapply.queue.DATA_DIR", tmp_path):
                _mark_deep_apply_done("li_nolog", "submitted", None)

        updated = json.loads(queue_file.read_text())
        assert "decision_log" not in updated[0]
