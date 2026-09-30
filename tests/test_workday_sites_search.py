"""The "workday" source: employers' own Workday sites, kept to US postings that
are remote or local to the applicant (built while LinkedIn had restricted the
applicant's account, 2026-09-30)."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobapply.profile import JobSearchParams  # noqa: E402
from jobapply.search import _posted_within, search_workday_sites  # noqa: E402

SITE = {"name": "Acme", "tenant": "acme", "wd": "wd1", "site": "Careers"}


def _posting(path, posted="Posted 2 Days Ago", title="Mechanical Engineer"):
    return {"title": title, "externalPath": path, "postedOn": posted, "locationsText": ""}


def _detail(country, location, remote=""):
    return {
        "jobPostingInfo": {
            "country": {"descriptor": country},
            "location": location,
            "remoteType": remote,
            "jobDescription": "<p>Design parts</p>",
        }
    }


def _run(postings, details, **params):
    base = dict(
        title="Mechanical Engineer",
        max_age_days=14,
        workday_sites=[SITE],
        local_terms=["indianapolis", ", in"],
    )
    base.update(params)
    with (
        patch(
            "jobapply.search._workday_search", return_value={"jobPostings": postings, "total": 9}
        ),
        patch("jobapply.search._workday_job_detail", side_effect=lambda t, w, s, p: details[p]),
        patch("jobapply.search.time.sleep"),
    ):
        return search_workday_sites(JobSearchParams(**base))


def test_keeps_local_and_remote_us_postings_only():
    postings = [_posting(p) for p in ("/job/a", "/job/b", "/job/c", "/job/d")]
    details = {
        "/job/a": _detail("United States of America", "Indianapolis, IN"),
        "/job/b": _detail("United States of America", "Denver, CO", remote="Remote"),
        "/job/c": _detail("United States of America", "Denver, CO"),
        # Lilly writes India as "IN: Hyderabad"; the country check keeps it out.
        "/job/d": _detail("India", "IN: Lilly Hyderabad"),
    }
    jobs = _run(postings, details)
    assert [j["url"].rsplit("/", 1)[-1] for j in jobs] == ["a", "b"]
    assert jobs[0]["description"] == "Design parts"
    assert jobs[0]["url"] == "https://acme.wd1.myworkdayjobs.com/Careers/job/a"
    assert jobs[1]["location"] == "Remote"
    assert all(j["apply_type"] == "external" and j["source"] == "workday" for j in jobs)


def test_old_excluded_and_blacklisted_postings_are_dropped():
    postings = [
        _posting("/job/old", posted="Posted 30+ Days Ago"),
        _posting("/job/intern", title="Mechanical Engineering Intern"),
    ]
    details = {
        p: _detail("United States of America", "Indianapolis, IN")
        for p in ("/job/old", "/job/intern")
    }
    assert _run(postings, details, keywords_excluded=["intern"]) == []
    assert (
        _run([_posting("/job/a")], {"/job/a": details["/job/old"]}, company_blacklist=["acme"])
        == []
    )


def test_posted_within():
    assert _posted_within("Posted Today", 14)
    assert _posted_within("Posted 14 Days Ago", 14)
    assert not _posted_within("Posted 15 Days Ago", 14)
    assert not _posted_within("Posted 30+ Days Ago", 14)
    assert _posted_within("", 14)
