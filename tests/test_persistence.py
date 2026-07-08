"""Tests for profile loading, credential storage, and application/search log persistence.

All file-touching tests use the data_dir fixture (conftest.py), which redirects
every path constant in job_search_apply to tmp_path.
"""

import json
import stat

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
