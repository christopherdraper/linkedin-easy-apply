"""Tests for post-batch failure analysis (batch_analysis.py)."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import batch_analysis  # noqa: E402
from batch_analysis import (  # noqa: E402
    _create_issue,
    _detect_ats_platform,
    _extract_validation_fields,
    _fingerprint_failure,
    _get_existing_issues,
    _load_applications,
    analyze,
)


@pytest.fixture
def ba_data_dir(tmp_path, monkeypatch):
    """Redirect batch_analysis path constants to tmp_path."""
    monkeypatch.setattr(batch_analysis, "DATA_DIR", tmp_path)
    monkeypatch.setattr(batch_analysis, "LOG_FILE", tmp_path / "applications.json")
    (tmp_path / "debug").mkdir()
    return tmp_path


def _failed_app(**overrides):
    app = {
        "job_id": "li_f1",
        "title": "Senior SRE",
        "company": "Acme",
        "status": "failed: captcha",
        "failure_category": "captcha",
        "ats_platform": "greenhouse",
        "match_score": 0.85,
        "timestamp": "2026-06-01 10:00:00",
    }
    app.update(overrides)
    return app


class TestLoadApplications:
    def test_missing_file_returns_empty(self, ba_data_dir):
        assert _load_applications() == []

    def test_since_filters_by_timestamp_string(self, ba_data_dir):
        apps = [
            _failed_app(job_id="old", timestamp="2026-05-01 10:00:00"),
            _failed_app(job_id="new", timestamp="2026-06-02 10:00:00"),
        ]
        (ba_data_dir / "applications.json").write_text(json.dumps(apps))
        result = _load_applications(since="2026-06-01")
        assert [a["job_id"] for a in result] == ["new"]

    def test_since_drops_entries_without_timestamp(self, ba_data_dir):
        # Suspicion: entries missing a timestamp default to "" which never
        # passes the >= since comparison, so they silently vanish.
        app = _failed_app(job_id="no_ts")
        del app["timestamp"]
        (ba_data_dir / "applications.json").write_text(json.dumps([app]))
        assert _load_applications(since="2026-01-01") == []
        assert len(_load_applications()) == 1


class TestExtractValidationFields:
    def test_extracts_fields_and_skips_generic_messages(self):
        status = "failed: First Name*; Please enter a valid first name; Email*"
        assert _extract_validation_fields(status) == ["First Name*", "Email*"]

    def test_strips_failed_and_validation_prefixes(self):
        status = "failed: form validation errors: Phone Number*"
        assert _extract_validation_fields(status) == ["Phone Number*"]

    def test_all_generic_returns_empty(self):
        status = "failed: Please enter a value; This field is required; There are problems"
        assert _extract_validation_fields(status) == []

    def test_long_fragments_dropped(self):
        long_part = "x" * 100
        status = f"failed: Email*; {long_part}"
        assert _extract_validation_fields(status) == ["Email*"]

    def test_field_names_containing_skip_words_are_dropped(self):
        # Suspicion: any field whose *name* contains a generic keyword like
        # "select" or "required" is dropped along with the boilerplate,
        # e.g. a real field literally named "Select your pronouns".
        status = "failed: Select your pronouns*; Email*"
        assert _extract_validation_fields(status) == ["Email*"]


class TestDetectAtsPlatform:
    def test_explicit_platform_returned(self):
        assert _detect_ats_platform({"ats_platform": "workday"}) == "workday"

    def test_unknown_platform_falls_through_to_debug_html(self, ba_data_dir):
        html = ba_data_dir / "debug" / "form_li_x1_step1.html"
        html.write_text("<html><body>Powered by Greenhouse</body></html>")
        app = {"ats_platform": "unknown", "job_id": "li_x1", "url": ""}
        assert _detect_ats_platform(app) == "greenhouse"

    def test_marker_priority_greenhouse_before_lever(self, ba_data_dir):
        html = ba_data_dir / "debug" / "form_li_x2.html"
        html.write_text("<html>lever greenhouse both mentioned</html>")
        app = {"ats_platform": "", "job_id": "li_x2", "url": ""}
        assert _detect_ats_platform(app) == "greenhouse"

    def test_url_fallback_when_no_debug_files(self, ba_data_dir):
        app = {"ats_platform": "", "job_id": "li_x3", "url": "https://jobs.lever.co/acme/123"}
        assert _detect_ats_platform(app) == "lever"

    def test_no_signal_returns_unknown(self, ba_data_dir):
        # Suspicion: the URL fallback only knows greenhouse/ashby/lever, so a
        # myworkdayjobs.com URL with no debug HTML still comes back unknown.
        app = {
            "ats_platform": "",
            "job_id": "li_x4",
            "url": "https://acme.wd1.myworkdayjobs.com/careers/job/1",
        }
        assert _detect_ats_platform(app) == "unknown"


class TestFingerprintFailure:
    def test_stable_for_equal_inputs(self):
        app_a = _failed_app()
        app_b = _failed_app()
        assert _fingerprint_failure(app_a) == _fingerprint_failure(app_b)
        assert _fingerprint_failure(app_a) == _fingerprint_failure(app_a)

    def test_validation_fields_normalized_and_sorted(self):
        app = _failed_app(
            failure_category="validation_error",
            status="failed: First Name*; Email*",
        )
        assert _fingerprint_failure(app) == "greenhouse:validation:email,first name"

    def test_validation_caps_at_three_fields(self):
        app = _failed_app(
            failure_category="validation_error",
            status="failed: Zeta*; Alpha*; Midway*; Beta*",
        )
        assert _fingerprint_failure(app) == "greenhouse:validation:alpha,beta,midway"

    def test_validation_spam_detection(self):
        app = _failed_app(
            failure_category="validation_error",
            status="failed: flagged as spam, please enter valid information",
        )
        assert _fingerprint_failure(app) == "greenhouse:validation:spam_detection"

    def test_validation_page_not_interactive(self):
        app = _failed_app(
            failure_category="validation_error",
            status="failed: wait until the page is loaded",
        )
        assert _fingerprint_failure(app) == "greenhouse:validation:page_not_interactive"

    def test_validation_unknown_fields_fallback(self):
        app = _failed_app(failure_category="validation_error", status="failed:")
        assert _fingerprint_failure(app) == "greenhouse:validation:unknown_fields"

    def test_form_stuck_variants(self):
        base = {"failure_category": "form_stuck", "ats_platform": "workday"}
        early = {**base, "status": "failed: form stuck at step 2/7"}
        mid = {**base, "status": "failed: form stuck at step 5/7"}
        loop = {**base, "status": "failed: same page after submit"}
        bare = {**base, "status": "failed: something odd"}
        assert _fingerprint_failure(early) == "workday:form_stuck:early_stall"
        assert _fingerprint_failure(mid) == "workday:form_stuck:mid_form"
        assert _fingerprint_failure(loop) == "workday:form_stuck:same_page_loop"
        assert _fingerprint_failure(bare) == "workday:form_stuck"

    def test_passthrough_categories(self):
        for cat in ("max_steps", "captcha", "no_apply_button"):
            app = _failed_app(failure_category=cat, status="failed: whatever")
            assert _fingerprint_failure(app) == f"greenhouse:{cat}"

    def test_other_category_status_breakdown(self):
        cases = [
            ("failed: invalid url for this posting", "greenhouse:invalid_url"),
            ("failed: requires account login", "greenhouse:login_wall"),
            ("failed: timeout after 300s", "greenhouse:timeout"),
        ]
        for status, expected in cases:
            app = _failed_app(failure_category="other", status=status)
            assert _fingerprint_failure(app) == expected

    def test_custom_category_ignores_status_breakdown(self):
        # Suspicion: the status-based breakdown only runs for
        # other/None/empty categories, so a custom category wins even when
        # the status clearly says timeout.
        app = _failed_app(failure_category="weird_category", status="failed: timeout after 300s")
        assert _fingerprint_failure(app) == "greenhouse:weird_category"

    def test_none_category_falls_back_to_uncategorized(self):
        app = _failed_app(failure_category=None, status="failed: mystery")
        assert _fingerprint_failure(app) == "greenhouse:uncategorized"

    def test_distinct_across_failure_shapes(self):
        apps = [
            _failed_app(failure_category="validation_error", status="failed: Email*"),
            _failed_app(failure_category="form_stuck", status="failed: form stuck at step 2/7"),
            _failed_app(failure_category="captcha", status="failed: captcha"),
            _failed_app(failure_category="max_steps", status="failed: max steps"),
            _failed_app(failure_category="other", status="failed: timeout after 300s"),
        ]
        fingerprints = [_fingerprint_failure(a) for a in apps]
        assert len(set(fingerprints)) == len(fingerprints)


class TestGetExistingIssues:
    def test_parses_bracketed_titles(self):
        issues = [
            {"title": "[greenhouse:captcha] Greenhouse captcha", "number": 1, "url": "u1"},
            {"title": "[lever:timeout] Lever timeout", "number": 2, "url": "u2"},
        ]
        result_mock = MagicMock(stdout=json.dumps(issues))
        with patch("batch_analysis.subprocess.run", return_value=result_mock) as run:
            existing = _get_existing_issues()
        assert set(existing.keys()) == {"greenhouse:captcha", "lever:timeout"}
        assert existing["greenhouse:captcha"]["number"] == 1
        assert run.call_args[0][0][:3] == ["gh", "issue", "list"]

    def test_ignores_titles_without_fingerprint_bracket(self):
        issues = [{"title": "random issue with no bracket", "number": 3, "url": "u3"}]
        result_mock = MagicMock(stdout=json.dumps(issues))
        with patch("batch_analysis.subprocess.run", return_value=result_mock):
            assert _get_existing_issues() == {}

    def test_subprocess_failure_returns_empty(self):
        err = subprocess.CalledProcessError(1, ["gh"])
        with patch("batch_analysis.subprocess.run", side_effect=err):
            assert _get_existing_issues() == {}


class TestCreateIssue:
    def test_dry_run_prints_and_skips_subprocess(self, ba_data_dir, capsys):
        apps = [_failed_app(), _failed_app(job_id="li_f2", match_score=0.9)]
        with patch("batch_analysis.subprocess.run") as run:
            result = _create_issue("greenhouse:captcha", apps, dry_run=True)
        assert result is None
        run.assert_not_called()
        out = capsys.readouterr().out
        assert "WOULD CREATE: [greenhouse:captcha] Greenhouse captcha" in out

    def test_validation_title_format(self, ba_data_dir, capsys):
        apps = [_failed_app()]
        _create_issue("greenhouse:validation:email,first name", apps, dry_run=True)
        out = capsys.readouterr().out
        assert (
            "WOULD CREATE: [greenhouse:validation:email,first name] "
            "Greenhouse form validation: email,first name" in out
        )

    def test_form_stuck_title_format(self, ba_data_dir, capsys):
        _create_issue("workday:form_stuck:early_stall", [_failed_app()], dry_run=True)
        out = capsys.readouterr().out
        assert (
            "WOULD CREATE: [workday:form_stuck:early_stall] Workday form stuck: early_stall" in out
        )

    def test_create_returns_url_and_passes_title(self, ba_data_dir):
        apps = [_failed_app()]
        result_mock = MagicMock(stdout="https://github.com/x/y/issues/12\n")
        with patch("batch_analysis.subprocess.run", return_value=result_mock) as run:
            url = _create_issue("greenhouse:max_steps", apps, dry_run=False)
        assert url == "https://github.com/x/y/issues/12"
        cmd = run.call_args[0][0]
        assert cmd[:3] == ["gh", "issue", "create"]
        title_idx = cmd.index("--title") + 1
        assert cmd[title_idx] == "[greenhouse:max_steps] Greenhouse exceeded max form steps"

    def test_subprocess_failure_returns_none(self, ba_data_dir):
        err = subprocess.CalledProcessError(1, ["gh"], stderr="boom")
        with patch("batch_analysis.subprocess.run", side_effect=err):
            assert _create_issue("greenhouse:captcha", [_failed_app()], dry_run=False) is None


class TestAnalyze:
    def test_no_failures_message(self, ba_data_dir, capsys):
        apps = [_failed_app(status="submitted")]
        (ba_data_dir / "applications.json").write_text(json.dumps(apps))
        analyze(dry_run=True)
        assert "No failures to analyze." in capsys.readouterr().out

    def test_dry_run_groups_and_flags_high_value_singles(self, ba_data_dir, capsys):
        apps = [
            _failed_app(job_id="f1"),
            _failed_app(job_id="f2", match_score=0.9),
            _failed_app(
                job_id="f3",
                ats_platform="lever",
                failure_category=None,
                status="failed: timeout after 300s",
                match_score=0.95,
            ),
            _failed_app(job_id="ok", status="submitted"),
        ]
        (ba_data_dir / "applications.json").write_text(json.dumps(apps))
        with patch("batch_analysis._get_existing_issues", return_value={}):
            analyze(dry_run=True, min_count=2)
        out = capsys.readouterr().out
        assert "Analyzing 3 failures" in out
        assert "2 distinct failure patterns" in out
        assert "WOULD CREATE: [greenhouse:captcha]" in out
        assert "1 single high-value failures" in out
        assert "lever:timeout" in out
