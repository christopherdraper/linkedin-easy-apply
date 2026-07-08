"""Tests for jobapply/search.py parsing: LinkedIn job cards (public layout,
malformed cards, dedup ids), RemoteOK JSON mapping, HackerNews Who's Hiring
comment parsing, the Workday CXS endpoints, and market snapshot failure
signaling."""

import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobapply.profile import JobSearchParams  # noqa: E402
from jobapply.search import (  # noqa: E402
    _count_failed_snapshots,
    _parse_job_cards,
    _workday_job_detail,
    _workday_search,
    market_snapshot,
    search_hn_whos_hiring,
    search_remoteok,
)


def _http_response(payload):
    """Context-manager mock mimicking urllib.request.urlopen returning JSON."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


# ---------------------------------------------------------------------------
# _parse_job_cards (branches not covered by TestParseJobCardsApplyType:
# public/guest layout, missing fields, malformed cards, session expiry,
# tracking-param canonicalization)
# ---------------------------------------------------------------------------


def _text_el(text):
    el = MagicMock()
    el.inner_text.return_value = text
    return el


def _public_card(title, company, href, easy_apply=False):
    """A div.job-search-card mock (public/guest LinkedIn layout)."""
    card = MagicMock()
    link_el = MagicMock()
    link_el.evaluate.return_value = href
    easy_el = MagicMock() if easy_apply else None
    card.query_selector.side_effect = lambda s: {
        "h3.base-search-card__title": _text_el(title),
        "a.base-card__full-link": link_el,
        "h4.base-search-card__subtitle": _text_el(company),
        ".job-search-card__location": _text_el("Remote"),
        ".job-search-card__easy-apply-label": easy_el,
    }.get(s)
    return card


def _auth_card(title, company, href, with_company=True):
    """A div.job-card-container mock (authenticated LinkedIn layout)."""
    card = MagicMock()
    title_el = _text_el(title)
    title_el.evaluate.return_value = href
    card.query_selector.side_effect = lambda s: {
        "a.job-card-list__title--link": title_el,
        "div.artdeco-entity-lockup__subtitle": _text_el(company) if with_company else None,
        "div.artdeco-entity-lockup__caption": _text_el("Remote"),
    }.get(s)
    card.query_selector_all.return_value = []  # no footer items -> external
    return card


def _public_page(cards):
    page = MagicMock()
    page.query_selector_all.side_effect = lambda sel: (
        [] if sel == "div.job-card-container" else cards
    )
    return page


class TestParseJobCards:
    def test_public_guest_layout_parsed_and_tagged(self):
        page = _public_page(
            [
                _public_card("SRE", "AlphaCo", "/jobs/view/999?refId=abc", easy_apply=True),
                _public_card("DevOps", "BetaCo", "https://www.linkedin.com/jobs/view/1000"),
            ]
        )
        jobs = _parse_job_cards(page)
        assert len(jobs) == 2
        # Relative href is absolutized against linkedin.com
        assert jobs[0]["url"] == "https://www.linkedin.com/jobs/view/999?refId=abc"
        assert jobs[0]["apply_type"] == "easy_apply"
        assert jobs[0]["title"] == "SRE"
        assert jobs[0]["company"] == "AlphaCo"
        assert jobs[0]["location"] == "Remote"
        assert jobs[0]["description"] == ""
        assert jobs[0]["id"].startswith("li_")
        # No easy-apply label -> external
        assert jobs[1]["apply_type"] == "external"

    def test_card_missing_company_is_skipped(self):
        page = MagicMock()
        page.query_selector_all.return_value = [
            _auth_card("SRE", "", "https://www.linkedin.com/jobs/view/1", with_company=False)
        ]
        assert _parse_job_cards(page) == []

    def test_malformed_card_skipped_but_others_kept(self):
        bad_card = MagicMock()
        bad_card.query_selector.side_effect = Exception("element detached")
        good_card = _auth_card("SRE", "GoodCo", "https://www.linkedin.com/jobs/view/2")
        page = MagicMock()
        page.query_selector_all.return_value = [bad_card, good_card]
        jobs = _parse_job_cards(page)
        assert len(jobs) == 1
        assert jobs[0]["company"] == "GoodCo"

    def test_selector_failure_raises_session_expired(self):
        page = MagicMock()
        page.query_selector_all.side_effect = Exception("Execution context was destroyed")
        with pytest.raises(RuntimeError, match="session expired"):
            _parse_job_cards(page)

    def test_tracking_params_stripped_for_stable_id(self):
        page = MagicMock()
        page.query_selector_all.return_value = [
            _auth_card("SRE", "TechCo", "https://www.linkedin.com/jobs/view/123?refId=a"),
            _auth_card("SRE", "TechCo", "https://www.linkedin.com/jobs/view/123?trk=b"),
        ]
        jobs = _parse_job_cards(page)
        assert len(jobs) == 2
        # Same canonical URL -> same id, but the original tracking URL is kept
        assert jobs[0]["id"] == jobs[1]["id"]
        assert jobs[0]["url"].endswith("?refId=a")
        assert jobs[1]["url"].endswith("?trk=b")


# ---------------------------------------------------------------------------
# search_remoteok
# ---------------------------------------------------------------------------


class TestSearchRemoteok:
    def test_maps_listing_to_job_dict(self):
        params = JobSearchParams(title="DevOps Engineer", max_age_days=None)
        data = [
            {"legal": "api metadata blob"},
            {
                "id": 456,
                "position": "DevOps Engineer",
                "company": "AcmeCo",
                "description": "Build and run infrastructure.",
                "url": "/remote-jobs/456",
                "apply_url": "/l/456",
            },
        ]
        with patch("urllib.request.urlopen", return_value=_http_response(data)) as mock_open:
            jobs = search_remoteok(params)

        req = mock_open.call_args[0][0]
        assert req.full_url == "https://remoteok.com/api?tag=devops-engineer"
        assert len(jobs) == 1
        job = jobs[0]
        assert job["id"] == "rok_456"
        # Relative URLs get the remoteok.com prefix
        assert job["url"] == "https://remoteok.com/l/456"
        assert job["listing_url"] == "https://remoteok.com/remote-jobs/456"
        assert job["title"] == "DevOps Engineer"
        assert job["company"] == "AcmeCo"
        assert job["description"] == "Build and run infrastructure."
        assert job["location"] == "Remote"
        assert job["easy_apply"] is False
        assert job["apply_type"] == "external"
        assert job["source"] == "remoteok"

    def test_filters_blacklist_exclusions_and_incomplete_items(self):
        params = JobSearchParams(
            title="devops",
            max_age_days=None,
            company_blacklist=["BadCo"],
            keywords_excluded=["manager"],
        )
        data = [
            {"legal": "metadata"},
            {"id": 1, "position": "DevOps Engineer", "company": "BadCo", "url": "/j/1"},
            {"id": 2, "position": "Engineering Manager", "company": "OkCo", "url": "/j/2"},
            {"id": 3, "position": "", "company": "OkCo", "url": "/j/3"},
            {"id": 4, "position": "DevOps Engineer", "company": "OkCo", "url": ""},
            {"id": 5, "position": "SRE", "company": "GoodCo", "url": "/j/5"},
        ]
        with patch("urllib.request.urlopen", return_value=_http_response(data)):
            jobs = search_remoteok(params)
        assert [j["id"] for j in jobs] == ["rok_5"]

    def test_api_error_returns_empty_list(self):
        params = JobSearchParams(title="devops")
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            assert search_remoteok(params) == []


# ---------------------------------------------------------------------------
# search_hn_whos_hiring
# ---------------------------------------------------------------------------


class TestSearchHnWhosHiring:
    def test_parses_thread_comments_into_jobs(self):
        params = JobSearchParams(title="devops engineer", max_age_days=None)
        thread_json = {
            "hits": [{"title": "Ask HN: Who is hiring? (July 2026)", "objectID": "44001"}]
        }
        comments_json = {
            "hits": [
                {
                    "objectID": "44002",
                    "comment_text": (
                        "AcmeCo | Senior DevOps Engineer | Remote (US)"
                        '<p>We need devops help. Apply: <a href="https://acme.example/jobs/1">here</a></p>'
                    ),
                },
                {
                    "objectID": "44003",
                    "comment_text": "BetaCo | SRE | Remote<p>Contact us by carrier pigeon.</p>",
                },
            ]
        }
        with patch(
            "urllib.request.urlopen",
            side_effect=[_http_response(thread_json), _http_response(comments_json)],
        ) as mock_open:
            jobs = search_hn_whos_hiring(params)

        assert mock_open.call_count == 2
        assert len(jobs) == 2
        first = jobs[0]
        assert first["id"] == "hn_44002"
        assert first["company"] == "AcmeCo"
        assert first["title"] == "Senior DevOps Engineer | Remote (US)"
        assert first["url"] == "https://acme.example/jobs/1"
        assert first["listing_url"] == "https://news.ycombinator.com/item?id=44002"
        assert first["apply_type"] == "external"
        assert first["source"] == "hackernews"
        assert "devops help" in first["description"]
        # No link in the comment -> URL falls back to the HN item page
        assert jobs[1]["url"] == "https://news.ycombinator.com/item?id=44003"

    def test_filters_irrelevant_and_blacklisted_comments(self):
        params = JobSearchParams(
            title="devops engineer",
            max_age_days=None,
            company_blacklist=["BadCo"],
            keywords_excluded=["clearance"],
        )
        thread_json = {"hits": [{"title": "Ask HN: Who is hiring? (July 2026)", "objectID": "1"}]}
        comments_json = {
            "hits": [
                # No "remote" mention
                {"objectID": "2", "comment_text": "OnSiteCo | DevOps Engineer | NYC office only"},
                # No matching keywords
                {
                    "objectID": "3",
                    "comment_text": "AcctCo | Senior Accountant | Remote bookkeeping",
                },
                # Excluded keyword
                {
                    "objectID": "4",
                    "comment_text": "SecCo | DevOps Engineer | Remote, requires clearance",
                },
                # Blacklisted company
                {"objectID": "5", "comment_text": "BadCo | DevOps Engineer | Remote, join us"},
            ]
        }
        with patch(
            "urllib.request.urlopen",
            side_effect=[_http_response(thread_json), _http_response(comments_json)],
        ):
            assert search_hn_whos_hiring(params) == []

    def test_no_matching_thread_returns_empty(self):
        params = JobSearchParams(title="devops engineer")
        thread_json = {
            "hits": [
                {"title": "Ask HN: Who is hiring? (Freelancer edition)", "objectID": "1"},
                {"title": "Show HN: who is hiring? tracker", "objectID": "2"},
            ]
        }
        with patch("urllib.request.urlopen", return_value=_http_response(thread_json)) as mock_open:
            assert search_hn_whos_hiring(params) == []
        # Never fetches comments when no valid thread was found
        assert mock_open.call_count == 1


# ---------------------------------------------------------------------------
# _workday_search / _workday_job_detail
# ---------------------------------------------------------------------------


class TestWorkdayEndpoints:
    def test_search_posts_query_and_returns_json(self):
        canned = {
            "total": 1,
            "jobPostings": [
                {
                    "title": "Senior SRE",
                    "externalPath": "/job/US/sre_R-100",
                    "locationsText": "US, Remote",
                    "postedOn": "Posted Today",
                    "bulletFields": ["R-100"],
                }
            ],
        }
        with patch("urllib.request.urlopen", return_value=_http_response(canned)) as mock_open:
            result = _workday_search("lilly", "wd5", "LLY", "sre remote", limit=5)

        assert result == canned
        req = mock_open.call_args[0][0]
        assert req.full_url == "https://lilly.wd5.myworkdayjobs.com/wday/cxs/lilly/LLY/jobs"
        assert req.get_method() == "POST"
        payload = json.loads(req.data.decode())
        assert payload == {"appliedFacets": {}, "limit": 5, "offset": 0, "searchText": "sre remote"}

    def test_search_error_returns_empty_dict(self):
        # Pins current behavior: the error path returns {} (not a list)
        with patch("urllib.request.urlopen", side_effect=OSError("boom")):
            assert _workday_search("lilly", "wd5", "LLY", "sre") == {}

    def test_job_detail_fetches_posting_info(self):
        canned = {
            "jobPostingInfo": {"jobDescription": "<p>Run the SRE team</p>", "remoteType": "Remote"}
        }
        with patch("urllib.request.urlopen", return_value=_http_response(canned)) as mock_open:
            result = _workday_job_detail("lilly", "wd5", "LLY", "/job/US/sre_R-100")

        assert result == canned
        req = mock_open.call_args[0][0]
        assert (
            req.full_url
            == "https://lilly.wd5.myworkdayjobs.com/wday/cxs/lilly/LLY/job/US/sre_R-100"
        )
        # Error path returns {}
        with patch("urllib.request.urlopen", side_effect=OSError("boom")):
            assert _workday_job_detail("lilly", "wd5", "LLY", "/job/US/sre_R-100") == {}


# ---------------------------------------------------------------------------
# Market snapshot failure signaling
# ---------------------------------------------------------------------------


def _snap(total, week, day):
    return {"total_results": total, "past_week_results": week, "past_day_results": day}


class TestCountFailedSnapshots:
    def test_empty_list_counts_zero(self):
        assert _count_failed_snapshots([]) == 0

    def test_all_counts_none_is_failed(self):
        assert _count_failed_snapshots([_snap(None, None, None)]) == 1

    def test_partial_counts_are_not_failed(self):
        snaps = [_snap(100, None, None), _snap(None, 5, None), _snap(None, None, 1)]
        assert _count_failed_snapshots(snaps) == 0

    def test_mixed_snapshots_counted_individually(self):
        snaps = [_snap(None, None, None), _snap(100, 50, 10), _snap(None, None, None)]
        assert _count_failed_snapshots(snaps) == 2

    def test_zero_counts_are_real_data_not_failures(self):
        assert _count_failed_snapshots([_snap(0, 0, 0)]) == 0


class TestMarketSnapshotFailureSignaling:
    def _run(self, titles, counts):
        """Run market_snapshot with all playwright collaborators mocked.

        `counts` feeds _extract_results_count: three values per title
        (all-time, past week, past day)."""
        page = MagicMock()
        with (
            patch("jobapply.search._stealth_playwright"),
            patch(
                "jobapply.search._playwright_context",
                return_value=(MagicMock(), MagicMock(), page, True),
            ),
            patch("jobapply.search._ensure_logged_in"),
            patch("jobapply.search._save_session"),
            patch("jobapply.search._extract_results_count", side_effect=counts),
            patch("jobapply.search.save_search_log") as save_log,
            patch("jobapply.search.time.sleep"),
        ):
            return market_snapshot(titles), save_log

    def test_all_titles_failed_returns_falsy_and_logs_error(self, caplog):
        with caplog.at_level(logging.ERROR, logger="jobapply.search"):
            result, save_log = self._run(["SRE", "DevOps"], [None] * 6)
        assert result == []
        assert not result  # the CLI maps falsy to exit(1)
        assert "session" in caplog.text.lower()
        # Snapshots are still persisted (gaps in the graph, not lost runs)
        assert save_log.call_count == 2

    def test_partial_failure_returns_snapshots_and_warns(self, caplog):
        counts = [100, 50, 10, None, None, None]
        with caplog.at_level(logging.WARNING, logger="jobapply.search"):
            result, _ = self._run(["SRE", "DevOps"], counts)
        assert len(result) == 2
        assert result[0]["total_results"] == 100
        assert result[1]["total_results"] is None
        assert "1 of 2" in caplog.text

    def test_all_titles_succeed_returns_snapshots_without_warnings(self, caplog):
        with caplog.at_level(logging.WARNING, logger="jobapply.search"):
            result, save_log = self._run(["SRE"], [100, 50, 10])
        assert len(result) == 1
        assert result[0]["total_results"] == 100
        assert result[0]["past_week_results"] == 50
        assert result[0]["past_day_results"] == 10
        assert "returned no counts" not in caplog.text
        assert save_log.call_count == 1
