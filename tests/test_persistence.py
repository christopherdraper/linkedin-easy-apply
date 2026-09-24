"""Tests for profile loading, credential storage, and application/search log persistence.

All file-touching tests use the data_dir fixture (conftest.py), which redirects
every path constant in job_search_apply to tmp_path.
"""

import json
import stat
import time

import pytest

from job_search_apply import (
    ApplicantProfile,
    _load_credentials,
    _save_credentials,
    already_applied,
    load_log,
    save_log,
    save_search_log,
)
from jobapply.applog import (
    SCORE_CACHE_TTL_DAYS,
    cached_score,
    load_score_cache,
    remember_score,
    save_score_cache,
)


class TestApplicantProfileFromJson:
    def _write_profile(self, path):
        data = {
            "profile": {
                "personal": {
                    "full_name": "Jane Doe",
                    "email": "jane@example.com",
                    "phone": "555-1234",
                    "location": {"city": "Austin", "state": "TX"},
                },
                "documents": {"resume_path": ""},
                "screening_answers": {"salary": "120000"},
            }
        }
        path.write_text(json.dumps(data))

    def test_valid_file_loads(self, tmp_path):
        profile_file = tmp_path / "profile.json"
        self._write_profile(profile_file)
        p = ApplicantProfile.from_json(str(profile_file))
        assert p.full_name == "Jane Doe"
        assert p.email == "jane@example.com"
        assert p.city == "Austin"
        assert p.screening_answers == {"salary": "120000"}

    def test_missing_file_raises(self, tmp_path):
        """from_json does no existence check; Path.read_text raises."""
        with pytest.raises(FileNotFoundError):
            ApplicantProfile.from_json(str(tmp_path / "does_not_exist.json"))

    def test_malformed_json_raises(self, tmp_path):
        """from_json does not guard json.loads; malformed content raises."""
        profile_file = tmp_path / "profile.json"
        profile_file.write_text("{not valid json")
        with pytest.raises(json.JSONDecodeError):
            ApplicantProfile.from_json(str(profile_file))


class TestCredentials:
    def test_round_trip(self, data_dir):
        _save_credentials("user@example.com", "s3cret")
        creds = _load_credentials()
        assert creds == {"email": "user@example.com", "password": "s3cret"}

    def test_missing_file_returns_none(self, data_dir):
        assert _load_credentials() is None

    def test_corrupt_json_returns_none(self, data_dir):
        (data_dir / "credentials.json").write_text("not json at all")
        assert _load_credentials() is None

    def test_blank_fields_return_none(self, data_dir):
        """Credentials with an empty email or password are treated as absent."""
        (data_dir / "credentials.json").write_text(json.dumps({"email": "", "password": "pw"}))
        assert _load_credentials() is None
        (data_dir / "credentials.json").write_text(json.dumps({"email": "a@b.com", "password": ""}))
        assert _load_credentials() is None

    def test_save_restricts_file_permissions(self, data_dir):
        _save_credentials("user@example.com", "s3cret")
        mode = stat.S_IMODE((data_dir / "credentials.json").stat().st_mode)
        assert mode == 0o600


class TestApplicationLog:
    def test_load_log_missing_returns_empty(self, data_dir):
        assert load_log() == []

    def test_save_and_load_round_trip(self, data_dir):
        entry = {"job_id": "li_1", "url": "https://a.com/jobs/1", "status": "submitted"}
        save_log([entry])
        assert load_log() == [entry]

    def test_load_log_corrupt_returns_empty(self, data_dir):
        (data_dir / "applications.json").write_text("{broken json")
        assert load_log() == []

    def test_save_log_appends_to_existing(self, data_dir):
        save_log([{"job_id": "li_1", "status": "submitted"}])
        save_log([{"job_id": "li_2", "status": "failed: form stuck"}])
        entries = load_log()
        assert len(entries) == 2
        assert entries[0]["job_id"] == "li_1"
        assert entries[1]["job_id"] == "li_2"

    def test_save_log_preserves_corrupt_existing(self, data_dir):
        """Fixed 2026-07-08: a corrupt applications.json is renamed aside
        (.corrupt) before the fresh write, so history bytes survive for
        manual recovery instead of being silently overwritten.
        """
        (data_dir / "applications.json").write_text("{broken json")
        save_log([{"job_id": "li_new", "status": "submitted"}])
        entries = load_log()
        assert entries == [{"job_id": "li_new", "status": "submitted"}]
        assert (data_dir / "applications.json.corrupt").read_text() == "{broken json"

    def test_save_log_caller_must_pass_only_new_entries(self, data_dir):
        """Regression guard for the append footgun.

        save_log() appends to what is already on disk. A caller that reads the
        log, appends its entry, and passes the WHOLE list back duplicates every
        existing record. This documents that contract so the anti-pattern is
        obvious to the next caller.
        """
        save_log([{"job_id": "li_1", "status": "submitted"}])
        # Correct usage: pass only the new entry.
        save_log([{"job_id": "li_2", "status": "submitted"}])
        assert len(load_log()) == 2

        # Anti-pattern: passing the full log back duplicates the history.
        everything = load_log()
        everything.append({"job_id": "li_3", "status": "submitted"})
        save_log(everything)
        assert len(load_log()) == 5  # 2 originals + 2 re-appended + 1 new


class TestExternalUrlLogging:
    """--external-url runs must add exactly one row to applications.json.

    Regression: the logging block passed the full log to save_log(), which
    appends, so every single-URL run duplicated the entire file (39 rows from
    20 real applications).
    """

    def test_logs_exactly_one_new_entry(self, data_dir, monkeypatch):
        from types import SimpleNamespace

        from jobapply import cli

        save_log([{"job_id": "old_1", "status": "submitted"}])

        profile_file = data_dir / "profile.json"
        profile_file.write_text(
            json.dumps(
                {
                    "profile": {
                        "personal": {"full_name": "Jane Doe", "email": "j@example.com"},
                        "documents": {"resume_path": ""},
                    }
                }
            )
        )

        monkeypatch.setattr(cli, "_abort_if_setup_incomplete", lambda profile: None)
        monkeypatch.setattr(
            cli, "submit_external_apply", lambda *a, **k: "review_parked: manual submit required"
        )

        cli._run_external_url(
            SimpleNamespace(
                profile=str(profile_file),
                external_url="https://acme.wd1.myworkdayjobs.com/job/1/apply",
                job_title="Engineer",
                company="Acme",
                proxy=None,
                dry_run=False,
            )
        )

        entries = load_log()
        assert len(entries) == 2, f"expected 1 new row, log grew to {len(entries)}"
        assert entries[0]["job_id"] == "old_1"


class TestScoreCache:
    """Scored-and-rejected jobs are cached so overlapping title searches stop
    paying to re-score them.

    Regression driver (2026-08-24): one batch spent 60 AI scoring calls on 29
    unique jobs (52% waste) because dedup only covered jobs already APPLIED to.
    """

    def _entry(self, **over):
        base = {
            "match_score": 0.62,
            "reasoning": "close but junior",
            "deal_breakers": [],
            "matched_skills": ["python"],
        }
        base.update(over)
        return base

    def test_round_trip(self, data_dir):
        cache = {}
        remember_score(cache, "li_1", self._entry(), ai_scored=True)
        save_score_cache(cache)
        assert load_score_cache()["li_1"]["match_score"] == 0.62

    def test_missing_file_returns_empty(self, data_dir):
        assert load_score_cache() == {}

    def test_corrupt_file_returns_empty(self, data_dir):
        (data_dir / "score_cache.json").write_text("{not json")
        assert load_score_cache() == {}

    def test_save_overwrites_rather_than_appends(self, data_dir):
        """Unlike save_log, the cache is a keyed store — no duplication."""
        cache = {}
        remember_score(cache, "li_1", self._entry(), ai_scored=True)
        save_score_cache(cache)
        remember_score(cache, "li_1", self._entry(match_score=0.9), ai_scored=True)
        save_score_cache(cache)
        reloaded = load_score_cache()
        assert len(reloaded) == 1
        assert reloaded["li_1"]["match_score"] == 0.9

    def test_fresh_entry_is_a_hit(self):
        cache = {}
        remember_score(cache, "li_1", self._entry(), ai_scored=True)
        assert cached_score(cache, "li_1")["match_score"] == 0.62

    def test_unknown_id_is_a_miss(self):
        assert cached_score({}, "li_nope") is None

    def test_expired_entry_is_a_miss(self):
        """A stale verdict must not blacklist a job forever."""
        cache = {}
        remember_score(cache, "li_1", self._entry(), ai_scored=True)
        cache["li_1"]["scored_at"] = time.time() - (SCORE_CACHE_TTL_DAYS + 1) * 86400
        assert cached_score(cache, "li_1") is None

    def test_keyword_fallback_entry_is_a_miss(self):
        """An AI outage produced that low score, not a real rejection."""
        cache = {}
        remember_score(cache, "li_1", self._entry(), ai_scored=False)
        assert cached_score(cache, "li_1") is None

    def test_stores_raw_score_not_a_reject_decision(self):
        """min_match_score is per-run, so a later lower bar must re-derive."""
        cache = {}
        remember_score(cache, "li_1", self._entry(match_score=0.72), ai_scored=True)
        entry = cached_score(cache, "li_1")
        assert entry["match_score"] == 0.72
        assert "rejected" not in entry

    def test_blank_job_id_is_not_stored(self):
        cache = {}
        remember_score(cache, "", self._entry(), ai_scored=True)
        assert cache == {}

    def test_malformed_entry_is_a_miss(self):
        assert cached_score({"li_1": {"ai_scored": True}}, "li_1") is None

    def test_verdict_from_different_scoring_rules_is_a_miss(self):
        """A scoring fix must take effect now, not in 14 days.

        Regression (2026-08-25): the CORE VS PERIPHERAL rule was added to lift
        jobs that were parking at 0.72 on peripheral gaps -- but the very jobs
        it targeted were being served from cache under the OLD rules, so the
        improvement would have been invisible until every entry aged out.
        """
        cache = {}
        remember_score(cache, "li_1", self._entry(), ai_scored=True, fingerprint="oldrules")
        assert cached_score(cache, "li_1", "newrules") is None
        assert cached_score(cache, "li_1", "oldrules")["match_score"] == 0.62

    def test_real_fingerprint_round_trips(self):
        from jobapply.content import SCORER_FINGERPRINT

        cache = {}
        remember_score(cache, "li_1", self._entry(), True, SCORER_FINGERPRINT)
        assert cached_score(cache, "li_1", SCORER_FINGERPRINT) is not None

    def test_fingerprint_tracks_the_scoring_rules(self):
        """Editing a rule must change the fingerprint automatically."""
        import hashlib

        from jobapply.content import _SCORING_RULES, SCORER_FINGERPRINT

        assert SCORER_FINGERPRINT == hashlib.sha256(_SCORING_RULES.encode()).hexdigest()[:12]
        assert "CORE VS PERIPHERAL" in _SCORING_RULES


class TestSearchLog:
    def test_creates_file_and_appends(self, data_dir):
        entry = {"title": "SRE", "results_count": 42}
        save_search_log(entry)
        saved = json.loads((data_dir / "search_log.json").read_text())
        assert saved == [entry]

    def test_appends_to_existing(self, data_dir):
        save_search_log({"title": "SRE", "results_count": 42})
        save_search_log({"title": "DevOps", "results_count": 7})
        saved = json.loads((data_dir / "search_log.json").read_text())
        assert len(saved) == 2
        assert saved[1]["title"] == "DevOps"

    def test_empty_file_treated_as_empty_list(self, data_dir):
        (data_dir / "search_log.json").write_text("")
        save_search_log({"title": "SRE", "results_count": 1})
        saved = json.loads((data_dir / "search_log.json").read_text())
        assert saved == [{"title": "SRE", "results_count": 1}]

    def test_corrupt_existing_preserved_and_write_continues(self, data_dir):
        """Fixed 2026-07-08: a corrupt search log is renamed aside
        (.corrupt) and the write proceeds fresh, instead of crashing every
        snapshot run.
        """
        (data_dir / "search_log.json").write_text("{broken json")
        save_search_log({"title": "SRE", "results_count": 1})
        saved = json.loads((data_dir / "search_log.json").read_text())
        assert saved == [{"title": "SRE", "results_count": 1}]
        assert (data_dir / "search_log.json.corrupt").read_text() == "{broken json"


class TestAlreadyAppliedReviewParked:
    """A Workday app parked at Review must never be re-driven.

    Regression (2026-08-25): already_applied matched only submitted/failed/
    skipped, so "review_parked: manual submit required" was invisible to dedup.
    The same Rolls-Royce req was driven to Review three times across two days,
    burning a vision pass (~$1.20, ~3 min) and a duplicate log row each time,
    and starving the batch of its remaining application budget.
    """

    def test_review_parked_counts_as_attempted(self):
        log = [
            {
                "url": "https://rr.wd3.myworkdayjobs.com/job/1",
                "job_id": "li_abc",
                "status": "review_parked: manual submit required",
            }
        ]
        result = already_applied(log)
        assert "https://rr.wd3.myworkdayjobs.com/job/1" in result
        assert "li_abc" in result

    def test_all_attempted_statuses_covered(self):
        for status in (
            "submitted",
            "failed: form stuck",
            "skipped: requires account",
            "review_parked: manual submit required",
        ):
            log = [{"url": "https://a.com/j/1", "job_id": "li_1", "status": status}]
            assert already_applied(log) == {"https://a.com/j/1", "li_1"}, status

    def test_unattempted_status_still_ignored(self):
        """A status we don't recognise must not silently block the job."""
        log = [{"url": "https://a.com/j/1", "job_id": "li_1", "status": "dry_run"}]
        assert already_applied(log) == set()


class TestAlreadyAppliedEdgeCases:
    """Edge cases beyond the basics in test_profile.py (status filtering,
    canonical query-param dedup, missing url)."""

    def test_trailing_slash_not_normalized(self):
        """Canonicalization only strips query strings; a trailing-slash
        variant of the same job does NOT match. Pins current behavior."""
        log = [{"url": "https://a.com/jobs/1/", "status": "submitted"}]
        result = already_applied(log)
        assert "https://a.com/jobs/1/" in result
        assert "https://a.com/jobs/1" not in result

    def test_case_not_normalized(self):
        """URL casing is preserved; a differently-cased URL for the same job
        does NOT match. Pins current behavior."""
        log = [{"url": "https://A.com/Jobs/1", "status": "submitted"}]
        result = already_applied(log)
        assert "https://A.com/Jobs/1" in result
        assert "https://a.com/jobs/1" not in result

    def test_fragment_not_stripped(self):
        """Only ?query is stripped, not #fragments, so a fragment variant of
        the same job does NOT match the bare URL. Pins current behavior."""
        log = [{"url": "https://a.com/jobs/1#apply", "status": "submitted"}]
        result = already_applied(log)
        assert "https://a.com/jobs/1#apply" in result
        assert "https://a.com/jobs/1" not in result

    def test_job_id_added_to_set(self):
        log = [{"job_id": "li_abc123", "status": "submitted"}]
        result = already_applied(log)
        assert "li_abc123" in result

    def test_query_string_with_no_path_slash(self):
        """Stripping '?...' from a URL whose path lacks a trailing slash
        yields the bare path, and both forms land in the set."""
        log = [{"url": "https://a.com/jobs/1?utm_source=x&eBP=y", "status": "failed: stuck"}]
        result = already_applied(log)
        assert "https://a.com/jobs/1?utm_source=x&eBP=y" in result
        assert "https://a.com/jobs/1" in result


class TestAtsAccountAnnouncement:
    """A generated password nobody was told about is a trap.

    Regression (2026-08-27): the bot created Workday accounts with 16-char
    generated passwords stored only in a 0600 file the applicant did not know
    existed. To submit his own parked applications he had to use "Forgot
    password", which invalidated the stored copy and locked the bot out of
    both tenants; each side then kept resetting past the other.
    """

    def test_new_account_is_announced_with_retrieval_instructions(self, data_dir, caplog):
        import logging

        from jobapply.accounts import _save_ats_account

        with caplog.at_level(logging.INFO):
            _save_ats_account("acme.wd1.myworkdayjobs.com", "a@b.com", "S3cret-Passw0rd!")
        out = caplog.text
        assert "ACCOUNT CREATED" in out
        assert "acme.wd1.myworkdayjobs.com" in out
        assert "--show-credentials" in out
        assert "Forgot password" in out

    def test_password_is_never_written_to_the_log(self, data_dir, caplog):
        import logging

        from jobapply.accounts import _save_ats_account

        with caplog.at_level(logging.INFO):
            _save_ats_account("acme.wd1.myworkdayjobs.com", "a@b.com", "S3cret-Passw0rd!")
        assert "S3cret-Passw0rd!" not in caplog.text

    def test_updating_an_existing_account_is_not_announced(self, data_dir, caplog):
        import logging

        from jobapply.accounts import _save_ats_account

        _save_ats_account("acme.wd1.myworkdayjobs.com", "a@b.com", "first")
        caplog.clear()
        with caplog.at_level(logging.INFO):
            _save_ats_account("acme.wd1.myworkdayjobs.com", "a@b.com", "second")
        assert "ACCOUNT CREATED" not in caplog.text

    def test_show_credentials_prints_the_login(self, data_dir, capsys):
        from jobapply.accounts import _save_ats_account
        from jobapply.cli import _show_ats_credentials

        _save_ats_account("acme.wd1.myworkdayjobs.com", "a@b.com", "S3cret-Passw0rd!")
        _show_ats_credentials()
        out = capsys.readouterr().out
        assert "acme.wd1.myworkdayjobs.com" in out
        assert "a@b.com" in out
        assert "S3cret-Passw0rd!" in out

    def test_show_credentials_handles_no_accounts(self, data_dir, capsys):
        from jobapply.cli import _show_ats_credentials

        _show_ats_credentials()
        assert "No ATS accounts" in capsys.readouterr().out


class TestRegistrationCredentialPersistence:
    """A generated ATS password must be persisted as soon as it is submitted,
    not only when the outcome heuristic reports success.

    UltiPro/Auth0 registration succeeded but the heuristic ("password field
    still visible") read it as a failure, so _save_ats_account never ran and
    the password was discarded. The account existed with a password nobody
    held, so every retry re-registered, got "The user already exists", and
    failed again -- an unrecoverable loop. Confirmed live 2026-09-16.
    """

    def test_password_saved_before_outcome_check(self, data_dir, profile):
        import jobapply.accounts as acc

        acc._save_ats_account("example-ats.com", profile.email, "GeneratedPw123!")
        stored = acc._load_ats_accounts()
        assert "example-ats.com" in stored
        assert stored["example-ats.com"]["password"] == "GeneratedPw123!"

    def test_resave_overwrites_rather_than_duplicating(self, data_dir, profile):
        import jobapply.accounts as acc

        acc._save_ats_account("example-ats.com", profile.email, "First1!aaa")
        acc._save_ats_account("example-ats.com", profile.email, "Second2!bbb")
        stored = acc._load_ats_accounts()
        assert len([k for k in stored if k == "example-ats.com"]) == 1
        assert stored["example-ats.com"]["password"] == "Second2!bbb"


class TestLinkedInWelcomeBackLogin:
    """LinkedIn renders two login layouts. The "Welcome back" variant
    remembers the account and shows ONLY a password field. _login_linkedin
    required both email and password, so auto-relogin aborted with
    "Could not find login form fields" on a form it could have completed,
    leaving the session dead. Observed live 2026-09-16.
    """

    def test_password_only_layout_proceeds(self, data_dir, monkeypatch):
        from unittest.mock import MagicMock

        import jobapply.browser as br

        monkeypatch.setattr(
            br, "_load_credentials", lambda: {"email": "e@x.com", "password": "pw"}
        )
        pw_field = MagicMock()
        # No email field (Welcome back), password field present.
        monkeypatch.setattr(
            br, "_first_visible", lambda page, sel: None if "username" in sel else pw_field
        )
        page = MagicMock()
        page.url = "https://www.linkedin.com/login/"
        br._login_linkedin(page)
        # The password must actually be entered rather than bailing early.
        pw_field.fill.assert_any_call("pw")

    def test_missing_password_field_still_fails(self, data_dir, monkeypatch):
        import jobapply.browser as br
        from unittest.mock import MagicMock

        monkeypatch.setattr(
            br, "_load_credentials", lambda: {"email": "e@x.com", "password": "pw"}
        )
        monkeypatch.setattr(br, "_first_visible", lambda page, sel: None)
        page = MagicMock()
        page.url = "https://www.linkedin.com/login/"
        assert br._login_linkedin(page) is False


class TestExternalUrlCoverLetter:
    """--external-url must generate a cover letter like batch runs do.

    Regression driver (2026-09-22): a single-URL run against a Greenhouse form
    with a required Cover Letter field looped until it burned its whole step
    budget, reporting "external form stuck (step N/20)". _run_external_url
    called submit_external_apply() without cover_letter_path, so it defaulted
    to "" and the required field was never filled. Validation failed forever.
    """

    def test_external_url_passes_cover_letter_path(self, data_dir, tmp_path, monkeypatch):
        import json
        from types import SimpleNamespace

        from jobapply import cli

        monkeypatch.setattr(cli, "DATA_DIR", data_dir)

        profile_file = data_dir / "profile.json"
        profile_file.write_text(
            json.dumps(
                {
                    "profile": {
                        "personal": {"full_name": "Jane Doe", "email": "j@example.com"},
                        "documents": {"resume_path": ""},
                    }
                }
            )
        )

        monkeypatch.setattr(cli, "_abort_if_setup_incomplete", lambda profile: None)
        monkeypatch.setattr(cli, "ai_generate_cover_letter", lambda job, profile: "Dear team,")
        cl_path = tmp_path / "cover.docx"
        cl_path.write_text("x")
        monkeypatch.setattr(cli, "_save_cover_letter_docx", lambda text, job_id: cl_path)

        captured = {}

        def fake_submit(job, profile, cover_letter_path="", **kwargs):
            captured["cover_letter_path"] = cover_letter_path
            return "submitted"

        monkeypatch.setattr(cli, "submit_external_apply", fake_submit)

        cli._run_external_url(
            SimpleNamespace(
                profile=str(profile_file),
                external_url="https://job-boards.greenhouse.io/oklo/jobs/1",
                job_title="Mechanical Design Engineer",
                company="Oklo",
                proxy=None,
                dry_run=False,
            )
        )

        assert captured.get("cover_letter_path"), (
            "submit_external_apply was called with no cover_letter_path; a form "
            "with a required Cover Letter field can never pass validation"
        )
        assert str(captured["cover_letter_path"]) == str(cl_path)


class TestAtsLoginRejectionDetection:
    """A rejected ATS login must be reported, not returned as a silent False.

    Regression driver (2026-09-24): Rolls-Royce's Workday answered the stored
    login with "You may have entered the wrong email address or password or
    your account might be locked." The routine only recognised "invalid",
    "incorrect", "wrong password" and "failed", so it logged nothing and a stale
    password looked like an unexplained failure.
    """

    WORKDAY_REJECTION = (
        "Sign In You may have entered the wrong email address or password "
        "or your account might be locked. Email Address* Password*"
    )

    def test_workday_rejection_is_reported(self, data_dir, caplog):
        import logging
        from unittest.mock import MagicMock, patch

        from jobapply import accounts

        domain = "rollsroyce.wd3.myworkdayjobs.com"
        accounts._save_ats_account(domain, "e@example.com", "stale-password")
        page = MagicMock()
        page.evaluate.return_value = self.WORKDAY_REJECTION.lower()
        page.url = f"https://{domain}/en-US/professional/login?redirect=x"
        with patch.object(accounts, "_safe_click"), caplog.at_level(logging.WARNING):
            assert accounts._attempt_ats_login(page, domain) is False
        assert any("rejected" in r.getMessage().lower() for r in caplog.records)

