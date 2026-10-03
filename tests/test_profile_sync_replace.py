"""Profile sync mirrors LinkedIn: trimmed history stays trimmed.

The applicant deliberately removed older roles and graduation details from
LinkedIn; a sync must not keep them in profile.json, where forms would use them.
"""

import json

from jobapply.cli import _apply_synced_profile


def test_roles_and_education_removed_on_linkedin_are_dropped(tmp_path):
    raw = {
        "profile": {
            "experience": {
                "previous_employers": [
                    {"employer": "INADEV", "title": "DevSecOps Engineer"},
                    {"employer": "Old Co", "title": "Systems Administrator"},
                ]
            },
            "education": {"university": "State U", "graduation_year": 2014, "minor": "Business"},
        }
    }
    parsed = {
        "previous_employers": [{"employer": "INADEV", "title": "DevSecOps Engineer"}],
        "education": {"university": "State U", "graduation_year": None},
    }
    path = tmp_path / "profile.json"
    _apply_synced_profile(raw, parsed, str(path))
    p = json.loads(path.read_text())["profile"]
    assert [e["employer"] for e in p["experience"]["previous_employers"]] == ["INADEV"]
    assert p["education"].get("graduation_year") is None
    assert "minor" not in p["education"]
