"""LinkedIn checkpoint/restriction detection: the batch must stop on the first
blocked page, record a cooldown, and never swallow the block as one failed job.

On 2026-09-29 LinkedIn restricted the account mid-batch and the batch hung on
the restriction page for an hour."""

import json
import sys
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jobapply.browser as browser  # noqa: E402
from jobapply import cli, watchdog  # noqa: E402
from jobapply.browser import (  # noqa: E402
    LinkedInBlockedError,
    _assert_linkedin_not_blocked,
    _ensure_logged_in,
    _linkedin_block_active,
    _linkedin_block_reason,
)
from jobapply.profile import JobSearchParams  # noqa: E402
from jobapply.search import _fetch_description  # noqa: E402
from jobapply.workflow import auto_apply_workflow  # noqa: E402

CHECKPOINT_URL = "https://www.linkedin.com/checkpoint/challenge/AgFv__EEF0Und?ut=3G-zp"
# Verbatim from the page LinkedIn served on 2026-09-29.
RESTRICTED_TEXT = (
    "Sign in Join now\nYour account has been temporarily restricted\n\n"
    "We restricted your account because we detected that over time, it has accessed "
    "an unusually high volume of LinkedIn profile data.\n\n"
    "Your restriction will be lifted on September 29, 2026 5:55 PM PDT.\n\n"
    "Think we got this wrong? Contact us.\n"
)


def _page(url, body=""):
    page = MagicMock()
    page.url = url
    page.evaluate.return_value = body
    return page


class TestBlockReason:
    def test_restriction_page_names_the_lift_time(self):
        reason = _linkedin_block_reason(_page(CHECKPOINT_URL, RESTRICTED_TEXT))
        assert reason.startswith("account temporarily restricted")
        assert "September 29, 2026 5:55 PM PDT" in reason

    def test_bare_checkpoint_url_is_a_block(self):
        reason = _linkedin_block_reason(_page(CHECKPOINT_URL, "Let's do a quick security check"))
        assert reason.startswith("security checkpoint")

    def test_restriction_text_on_another_linkedin_url_is_a_block(self):
        reason = _linkedin_block_reason(
            _page("https://www.linkedin.com/jobs/view/1", RESTRICTED_TEXT)
        )
        assert reason.startswith("account temporarily restricted")

    def test_normal_jobs_page_is_not_a_block(self):
        page = _page("https://www.linkedin.com/jobs/search/?keywords=x", "About the job")
        assert _linkedin_block_reason(page) is None

    def test_ats_page_is_never_a_linkedin_block(self):
        page = _page("https://acme.wd1.myworkdayjobs.com/checkpoint/", RESTRICTED_TEXT)
        assert _linkedin_block_reason(page) is None
        page.evaluate.assert_not_called()


class TestCooldownRecord:
    def test_assert_records_the_block_and_raises(self):
        with pytest.raises(LinkedInBlockedError, match="restricted"):
            _assert_linkedin_not_blocked(_page(CHECKPOINT_URL, RESTRICTED_TEXT))
        rec = _linkedin_block_active()
        assert rec is not None and "restricted" in rec["reason"]

    def test_block_expires_after_the_cooldown(self):
        old = datetime.now(timezone.utc) - timedelta(hours=browser.LINKEDIN_BLOCK_COOLDOWN_H + 1)
        browser.LINKEDIN_BLOCK_FILE.write_text(json.dumps({"at": old.isoformat(), "reason": "x"}))
        assert _linkedin_block_active() is None

    def test_missing_or_corrupt_record_is_not_a_block(self):
        assert _linkedin_block_active() is None
        browser.LINKEDIN_BLOCK_FILE.write_text("{not json")
        assert _linkedin_block_active() is None


class TestNoCallerSwallowsTheBlock:
    def test_ensure_logged_in_raises_without_trying_to_log_in(self):
        with patch("jobapply.browser._login_linkedin") as m_login:
            with pytest.raises(LinkedInBlockedError):
                _ensure_logged_in(_page(CHECKPOINT_URL, RESTRICTED_TEXT), "https://x")
        m_login.assert_not_called()

    def test_description_fetch_propagates_the_block(self):
        context = MagicMock()
        context.new_page.return_value = _page(CHECKPOINT_URL, RESTRICTED_TEXT)
        with pytest.raises(LinkedInBlockedError):
            _fetch_description(context, "https://www.linkedin.com/jobs/view/1")

    def test_easy_apply_propagates_the_block(self, profile, job, playwright_ctx):
        from jobapply import easy_apply

        page = _page(CHECKPOINT_URL, RESTRICTED_TEXT)
        with (
            patch("jobapply.easy_apply._stealth_playwright"),
            patch(
                "jobapply.easy_apply._playwright_context",
                return_value=(MagicMock(), MagicMock(), page, False),
            ),
        ):
            with pytest.raises(LinkedInBlockedError):
                easy_apply.submit_easy_apply(job, profile)

    def test_external_apply_propagates_the_block(self, profile, job, playwright_ctx):
        from jobapply.external import submit_external_apply

        page = _page(CHECKPOINT_URL, RESTRICTED_TEXT)
        with playwright_ctx(page):
            with patch("jobapply.external._ensure_logged_in", side_effect=_ensure_logged_in):
                with pytest.raises(LinkedInBlockedError):
                    submit_external_apply(job, profile)

    def test_workflow_propagates_a_block_from_the_search(self, data_dir, profile):
        with patch("jobapply.workflow._search_source", side_effect=LinkedInBlockedError("blocked")):
            with pytest.raises(LinkedInBlockedError):
                auto_apply_workflow(JobSearchParams(title="x"), profile)


def _args():
    return Namespace(source="linkedin", location=None, dry_run=False, proxy=None)


class TestBatchStops:
    def test_block_mid_batch_stops_every_remaining_title_and_exits_75(self, profile):
        with (
            patch(
                "jobapply.cli.auto_apply_workflow", side_effect=LinkedInBlockedError("blocked")
            ) as m_wf,
            patch("jobapply.cli.time.sleep"),
            patch("jobapply.cli.watchdog") as m_wd,
        ):
            with pytest.raises(SystemExit) as exc:
                cli._run_batch(_args(), profile, {}, 5, 0.6, ["A", "B", "C"], False)
        assert exc.value.code == cli.EXIT_LINKEDIN_BLOCKED
        assert m_wf.call_count == 1
        m_wd.stop.assert_called_once()

    def test_active_block_refuses_to_start(self, profile):
        browser._record_linkedin_block("account temporarily restricted")
        with patch("jobapply.cli.auto_apply_workflow") as m_wf:
            with pytest.raises(SystemExit) as exc:
                cli._run_batch(_args(), profile, {}, 5, 0.6, ["A"], False)
        assert exc.value.code == cli.EXIT_LINKEDIN_BLOCKED
        m_wf.assert_not_called()

    def test_active_block_still_runs_other_sources(self, profile):
        browser._record_linkedin_block("account temporarily restricted")
        args = _args()
        args.source = "all"
        with (
            patch("jobapply.cli.auto_apply_workflow") as m_wf,
            patch("jobapply.cli.time.sleep"),
            patch("jobapply.cli.watchdog"),
        ):
            cli._run_batch(args, profile, {}, 5, 0.6, ["A"], False)
        sources = [c.kwargs["source"] for c in m_wf.call_args_list]
        assert "linkedin" not in sources and sources


class TestWatchdog:
    def test_pet_is_a_no_op_until_started(self):
        with patch("jobapply.watchdog.faulthandler") as m_fh:
            watchdog.pet()
        m_fh.dump_traceback_later.assert_not_called()

    def test_start_arms_an_exiting_timer_and_stop_cancels_it(self):
        with patch("jobapply.watchdog.faulthandler") as m_fh:
            watchdog.start(timeout_s=123)
            watchdog.pet()
            watchdog.stop()
        call = m_fh.dump_traceback_later.call_args
        assert call.args[0] == 123 and call.kwargs["exit"] is True
        assert m_fh.dump_traceback_later.call_count == 2
        m_fh.cancel_dump_traceback_later.assert_called_once()
        with patch("jobapply.watchdog.faulthandler") as m_fh:
            watchdog.pet()
        m_fh.dump_traceback_later.assert_not_called()
