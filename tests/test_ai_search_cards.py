"""LinkedIn's AI-search results layout (/jobs/search-results/), seen 2026-10-03.

Cards carry no stable classes and no links, only
componentkey="job-card-component-ref-<jobId>", so the classic parser found
nothing and every search reported "No results found".
"""

import hashlib
from unittest.mock import MagicMock

from jobapply import search as S

# Paragraph texts as read from a live card.
KINTSUGI = [
    "Selected, Senior Site Reliability Engineer (DevOps)\nSenior Site Reliability Engineer (DevOps)",
    "Kintsugi",
    "United States (Remote)",
    "$180K/yr - $190K/yr",
    "You’d be a top applicant",
    "Posted 2 days ago\n2 days ago",
]


def test_card_fields_come_from_paragraph_order():
    job = S._ai_search_job("4473974160", KINTSUGI)
    assert job["title"] == "Senior Site Reliability Engineer (DevOps)"
    assert job["company"] == "Kintsugi"
    assert job["location"] == "United States (Remote)"
    assert job["apply_type"] == "external"


def test_job_id_matches_classic_layout_hash():
    # Classic cards hashed the canonical /jobs/view/<id>/ URL; matching it keeps
    # already-applied jobs and cached scores recognised across layouts.
    url = "https://www.linkedin.com/jobs/view/4473974160/"
    job = S._ai_search_job("4473974160", KINTSUGI)
    assert job["url"] == url
    assert job["id"] == f"li_{hashlib.sha256(url.encode()).hexdigest()[:12]}"


def test_easy_apply_and_verified_suffix():
    paras = [
        "Staff Site Reliability Engineer (Verified job)\nStaff Site Reliability Engineer (Verified job)",
        "BlinkRx",
        "United States (Remote)",
        "Easy Apply",
    ]
    job = S._ai_search_job("4470000001", paras)
    assert job["title"] == "Staff Site Reliability Engineer"
    assert job["apply_type"] == "easy_apply"


def test_malformed_cards_are_dropped():
    assert S._ai_search_job("not-a-number", KINTSUGI) is None
    assert S._ai_search_job("4473974160", ["Only a title"]) is None


def test_parse_job_cards_dispatches_to_ai_layout():
    page = MagicMock()
    page.query_selector_all.return_value = []
    page.query_selector.side_effect = lambda sel: object() if "componentkey" in sel else None
    page.eval_on_selector_all.return_value = [
        {"jobId": "4473974160", "paras": KINTSUGI},
        {"jobId": "bad", "paras": KINTSUGI},
    ]
    jobs = S._parse_job_cards(page)
    assert [j["company"] for j in jobs] == ["Kintsugi"]
