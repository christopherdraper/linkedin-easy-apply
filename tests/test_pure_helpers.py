"""Tests for pure helper functions in job_search_apply."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_search_apply import (  # noqa: E402
    _best_option_match,
    _categorize_failure,
    _clamp_to_maxlength,
    _extract_code_from_email_body,
    _extract_results_count,
)

# ---------------------------------------------------------------------------
# _categorize_failure tests
# ---------------------------------------------------------------------------


class TestCategorizeFailure:
    def test_spam_maps_to_captcha(self):
        assert _categorize_failure("failed: spam detected") == "captcha"

    def test_recaptcha_maps_to_captcha(self):
        assert _categorize_failure("recaptcha challenge served") == "captcha"

    def test_security_check_case_insensitive(self):
        assert _categorize_failure("SECURITY CHECK triggered") == "captcha"

    def test_spam_inside_validation_error_wins(self):
        # Ordering invariant: spam/captcha are checked BEFORE validation_error
        # because spam flags often arrive wrapped in a validation error message.
        assert _categorize_failure("failed: validation error (spam detected)") == "captcha"

    def test_captcha_inside_validation_error_wins(self):
        assert _categorize_failure("validation error: captcha required") == "captcha"

    def test_form_stuck(self):
        assert _categorize_failure("form stuck on step 2") == "form_stuck"
        assert _categorize_failure("lost track of form steps") == "form_stuck"
        assert _categorize_failure("page not progressing") == "form_stuck"

    def test_form_stuck_checked_before_validation_error(self):
        assert _categorize_failure("form stuck after validation error") == "form_stuck"

    def test_validation_error(self):
        assert _categorize_failure("failed: validation error on submit") == "validation_error"

    def test_no_apply_button(self):
        assert _categorize_failure("no apply button found on listing") == "no_apply_button"

    def test_login_wall(self):
        assert _categorize_failure("requires account creation") == "login_wall"
        assert _categorize_failure("stuck at login page") == "login_wall"

    def test_login_checked_before_timeout(self):
        # A timeout while stuck at a login wall routes to login_wall because
        # the login check runs before the timeout check.
        assert _categorize_failure("timed out waiting for login page") == "login_wall"

    def test_modal_lost(self):
        assert _categorize_failure("modal disappeared") == "modal_lost"

    def test_max_steps(self):
        assert _categorize_failure("hit max steps") == "max_steps"
        assert _categorize_failure("gave up: too many redirects") == "max_steps"

    def test_timeout(self):
        assert _categorize_failure("navigation timeout") == "timeout"
        assert _categorize_failure("page timed out") == "timeout"

    def test_unrecognized_falls_back_to_other(self):
        assert _categorize_failure("something odd happened") == "other"
        assert _categorize_failure("") == "other"


# ---------------------------------------------------------------------------
# _best_option_match tests
# ---------------------------------------------------------------------------


class TestBestOptionMatch:
    def test_exact_match_case_insensitive(self):
        options = ["United States", "United States of America"]
        assert _best_option_match("united states", options) == 0

    def test_united_states_regression(self):
        # Regression from the docstring: "United States" must pick
        # "United States of America", not "United States Minor Outlying
        # Islands". Both contain the answer; the tie is broken by the
        # smaller length difference.
        options = ["United States Minor Outlying Islands", "United States of America"]
        assert _best_option_match("United States", options) == 1

    def test_option_substring_of_answer(self):
        options = ["Bachelor", "Doctorate"]
        assert _best_option_match("Bachelor's Degree in CS", options) == 0

    def test_prefix_match(self):
        # No substring either way; only the 6-char prefix rule fires.
        options = ["Bachelor Degree", "Master Degree"]
        assert _best_option_match("Bachelor's in CS", options) == 0

    def test_tie_broken_by_shorter_option(self):
        options = ["Senior Software Engineer II", "Senior Engineer"]
        assert _best_option_match("Senior", options) == 1

    def test_no_match_returns_minus_one(self):
        # Fixed 2026-07-08: the tie-break used to fire at score 0 and return
        # the closest-length option for a totally unrelated answer. Now the
        # docstring's promised -1 actually happens, and callers skip the
        # field instead of selecting an arbitrary option.
        assert _best_option_match("xyz", ["abc", "defghi"]) == -1

    def test_empty_options_returns_minus_one(self):
        assert _best_option_match("anything", []) == -1


# ---------------------------------------------------------------------------
# _clamp_to_maxlength tests
# ---------------------------------------------------------------------------


class TestClampToMaxlength:
    def test_truncates_to_maxlength(self):
        inp = MagicMock()
        inp.get_attribute.return_value = "5"
        assert _clamp_to_maxlength(inp, "abcdefghij") == "abcde"
        assert _clamp_to_maxlength(inp, "abc") == "abc"  # already short enough

    def test_no_maxlength_returns_value_unchanged(self):
        inp = MagicMock()
        inp.get_attribute.return_value = None
        assert _clamp_to_maxlength(inp, "abcdefghij") == "abcdefghij"

    def test_non_numeric_maxlength_returns_value_unchanged(self):
        inp = MagicMock()
        inp.get_attribute.return_value = "abc"
        assert _clamp_to_maxlength(inp, "abcdefghij") == "abcdefghij"


# ---------------------------------------------------------------------------
# _extract_code_from_email_body tests
# ---------------------------------------------------------------------------


class TestExtractCodeFromEmailBody:
    def test_greenhouse_colon_format(self):
        assert _extract_code_from_email_body("Your security code is: 483920") == "483920"

    def test_code_split_across_newlines(self):
        body = "Your security\ncode\nis:\n 774411\nThanks"
        assert _extract_code_from_email_body(body) == "774411"

    def test_no_code_returns_none(self):
        assert _extract_code_from_email_body("Thanks for applying! We will be in touch.") is None

    def test_multiple_numbers_picks_keyword_context(self):
        body = "Please enter code 553311 to verify. Ref 20260708."
        assert _extract_code_from_email_body(body) == "553311"

    def test_plain_words_near_keywords_are_skipped(self):
        body = "Please verify your account by clicking the link below."
        assert _extract_code_from_email_body(body) is None

    def test_mixed_case_alpha_code_accepted(self):
        assert _extract_code_from_email_body("Enter the code AbCdEf to continue.") == "AbCdEf"

    def test_capitalized_code_keyword_uses_context_fallback(self):
        # The primary regex is case-sensitive on "code"; "Code: 123456" is
        # caught by the keyword-context fallback instead.
        assert _extract_code_from_email_body("Code: 123456") == "123456"


# ---------------------------------------------------------------------------
# _extract_results_count tests
# ---------------------------------------------------------------------------


class TestExtractResultsCount:
    def test_parses_comma_separated_count(self, make_page):
        page = make_page(body_text="1,234 results")
        assert _extract_results_count(page) == 1234

    def test_parses_plus_suffixed_count(self, make_page):
        page = make_page(body_text="4,000+")
        assert _extract_results_count(page) == 4000

    def test_returns_none_when_selectors_fail(self, make_page):
        page = make_page(body_text="")
        assert _extract_results_count(page) is None


# ---------------------------------------------------------------------------
# _hydrate_job_list tests
# ---------------------------------------------------------------------------


class _FakeResultsPage:
    """Stands in for a Playwright page whose job list renders as it scrolls."""

    def __init__(self, counts):
        self._counts = list(counts)
        self.scripts = []
        self.waits = 0

    def evaluate(self, script):
        self.scripts.append(script)
        # Hold the last value once the sequence is exhausted (list fully rendered).
        return self._counts.pop(0) if len(self._counts) > 1 else self._counts[0]

    def wait_for_timeout(self, ms):
        self.waits += 1


class TestHydrateJobList:
    """LinkedIn's authenticated results list is virtualized: all ~25 slots exist
    on load but only ~7 render a card until the results PANE scrolls.

    Regression driver (2026-09-23): search_linkedin scrolled the window once,
    so every search parsed 7 of 25 results and silently discarded 72% of them.
    Live probe: window scroll -> 7 cards; pane scroll -> 25 of 25.
    """

    def test_scrolls_until_card_count_stops_growing(self):
        from jobapply.search import _hydrate_job_list

        page = _FakeResultsPage([7, 14, 21, 25, 25, 25, 25])
        assert _hydrate_job_list(page) == 25
        # Stops once stable instead of burning every step.
        assert len(page.scripts) < 15

    def test_gives_up_after_max_steps(self):
        from jobapply.search import _hydrate_job_list

        page = _FakeResultsPage(list(range(1, 100)))
        _hydrate_job_list(page, max_steps=5)
        assert len(page.scripts) == 5

    def test_scrolls_the_results_pane_not_the_window(self):
        from jobapply.search import _SCROLL_JOB_PANE_JS, _hydrate_job_list

        page = _FakeResultsPage([7, 7, 7])
        _hydrate_job_list(page)
        assert page.scripts[0] == _SCROLL_JOB_PANE_JS
        # The pane's class names are obfuscated, so it must be found by overflow.
        assert "overflowY" in _SCROLL_JOB_PANE_JS
        assert "window.scroll" not in _SCROLL_JOB_PANE_JS


# ---------------------------------------------------------------------------
# _assert_linkedin_session tests
# ---------------------------------------------------------------------------


class _FakeCtx:
    def __init__(self, names):
        self._names = names

    def cookies(self, *urls):
        return [{"name": n, "domain": ".linkedin.com"} for n in self._names]


class _FakeSearchPage:
    def __init__(self, authed_cards, guest_cards):
        self._sel = {
            "div.job-card-container": object() if authed_cards else None,
            "div.job-search-card": object() if guest_cards else None,
        }

    def query_selector(self, sel):
        return self._sel.get(sel)


class TestAssertLinkedInSession:
    """A revoked LinkedIn session does not redirect job search to the authwall:
    LinkedIn serves PUBLIC results, the parser falls back to the guest card
    layout, and every application then fails on a sign-in wall. On 2026-09-23 a
    batch ran ~an hour like this. Either signal alone must stop the search.
    """

    def test_authenticated_session_passes(self):
        from jobapply.search import _assert_linkedin_session

        _assert_linkedin_session(_FakeCtx(["li_at", "JSESSIONID"]), _FakeSearchPage(True, False))

    def test_missing_li_at_raises_session_expired(self):
        import pytest

        from jobapply.search import _assert_linkedin_session

        with pytest.raises(RuntimeError, match="session expired"):
            _assert_linkedin_session(_FakeCtx(["JSESSIONID"]), _FakeSearchPage(True, False))

    def test_public_guest_layout_raises_session_expired(self):
        import pytest

        from jobapply.search import _assert_linkedin_session

        with pytest.raises(RuntimeError, match="session expired"):
            _assert_linkedin_session(_FakeCtx(["li_at"]), _FakeSearchPage(False, True))


# ---------------------------------------------------------------------------
# _fetch_descriptions tests (LinkedIn page-load throttle)
# ---------------------------------------------------------------------------


class _PacingPage:
    def __init__(self):
        self.waits = []

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


class TestFetchDescriptionsThrottle:
    """Skip already-decided jobs and pace the rest. On 2026-09-23 ~25 unpaced
    LinkedIn job-page loads per search preceded a session revocation."""

    def _jobs(self, n):
        return [{"id": f"li_{i}", "url": f"https://www.linkedin.com/jobs/view/{i}/", "description": ""}
                for i in range(n)]

    def test_fetches_only_jobs_that_need_a_description(self, monkeypatch):
        from jobapply import search

        fetched = []
        monkeypatch.setattr(search, "_fetch_description",
                            lambda ctx, url: (fetched.append(url) or ("desc", "1 day ago")))
        jobs = self._jobs(5)
        n = search._fetch_descriptions(None, _PacingPage(), jobs,
                                       need_description=lambda j: j["id"] in ("li_1", "li_3"))
        assert n == 2
        assert fetched == [jobs[1]["url"], jobs[3]["url"]]
        assert jobs[0]["description"] == "" and jobs[0]["posted_ago"] == ""
        assert jobs[1]["description"] == "desc"

    def test_paces_between_page_loads(self, monkeypatch):
        from jobapply import search

        monkeypatch.setattr(search, "_fetch_description", lambda ctx, url: ("d", ""))
        page = _PacingPage()
        search._fetch_descriptions(None, page, self._jobs(4), gap_s=(2.0, 5.0))
        assert len(page.waits) == 3  # a gap between each pair, none before the first
        assert all(2000 <= ms <= 5000 for ms in page.waits)

    def test_no_filter_fetches_everything(self, monkeypatch):
        from jobapply import search

        monkeypatch.setattr(search, "_fetch_description", lambda ctx, url: ("d", ""))
        assert search._fetch_descriptions(None, _PacingPage(), self._jobs(3)) == 3

