"""Profile sync must not delete what the LinkedIn profile page did not show.

On 2026-10-03 a sync replaced 5 stored roles with the 3 the page rendered and
wiped graduation year, minor and continuing education.
"""

import json

from jobapply.cli import _apply_synced_profile


def _stored():
    return {
        "profile": {
            "experience": {
                "previous_employers": [
                    {"employer": "INADEV", "title": "DevSecOps Engineer", "dates": "2023 - 2024"},
                    {"employer": "Ball State University", "title": "Network Analyst"},
                    {"employer": "Ball State University", "title": "Systems Administrator"},
                ]
            },
            "education": {
                "university": "Ball State University",
                "graduation_year": 2014,
                "minor": "Business Administration",
            },
        }
    }


def test_partial_parse_keeps_unshown_roles_and_education(tmp_path):
    path = tmp_path / "profile.json"
    parsed = {
        "previous_employers": [
            {"employer": "inadev", "title": "DevSecOps engineer", "dates": "Aug 2023 - Dec 2024"}
        ],
        "education": {"university": "Ball State University", "graduation_year": None},
    }
    _apply_synced_profile(_stored(), parsed, str(path))
    p = json.loads(path.read_text())["profile"]

    roles = [(e["employer"], e["title"]) for e in p["experience"]["previous_employers"]]
    # Parsed entry first and updated, not duplicated; unshown roles kept after it.
    assert roles == [
        ("inadev", "DevSecOps engineer"),
        ("Ball State University", "Network Analyst"),
        ("Ball State University", "Systems Administrator"),
    ]
    assert p["experience"]["previous_employers"][0]["dates"] == "Aug 2023 - Dec 2024"
    assert p["education"]["graduation_year"] == 2014
    assert p["education"]["minor"] == "Business Administration"


def test_new_values_still_update(tmp_path):
    path = tmp_path / "profile.json"
    parsed = {"education": {"graduation_year": 2015, "field_of_study": "CIT"}}
    _apply_synced_profile(_stored(), parsed, str(path))
    edu = json.loads(path.read_text())["profile"]["education"]
    assert edu["graduation_year"] == 2015
    assert edu["field_of_study"] == "CIT"
    assert edu["minor"] == "Business Administration"
