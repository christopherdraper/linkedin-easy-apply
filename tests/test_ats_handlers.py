"""Tests for ATS handler base class and default handler."""

from unittest.mock import MagicMock, patch

import pytest

from ats_handlers._base import BaseATSHandler
from ats_handlers.default import DefaultHandler


class TestBaseATSHandlerInstantiation:
    def test_base_cannot_be_instantiated_directly(self):
        """BaseATSHandler is abstract and must raise TypeError on direct instantiation."""
        with pytest.raises(TypeError):
            BaseATSHandler()

    def test_subclass_without_platform_name_cannot_be_instantiated(self):
        """A concrete subclass that omits platform_name must also raise TypeError."""

        class IncompleteHandler(BaseATSHandler):
            pass

        with pytest.raises(TypeError):
            IncompleteHandler()


class TestDefaultHandler:
    def test_default_handler_instantiates(self):
        handler = DefaultHandler()
        assert handler is not None

    def test_platform_name_is_unknown(self):
        handler = DefaultHandler()
        assert handler.platform_name == "unknown"


class TestDefaultHandlerHooks:
    """All no-op hooks return their documented default values."""

    def setup_method(self):
        self.handler = DefaultHandler()
        self.page = object()  # opaque placeholder -- hooks must not inspect it
        self.ctx = {}

    # Q1 hooks

    def test_pre_flight_returns_none(self):
        result = self.handler.pre_flight(self.page, self.ctx)
        assert result is None

    def test_on_step_start_returns_none(self):
        result = self.handler.on_step_start(self.page, self.ctx)
        assert result is None

    def test_resolve_login_wall_returns_false(self):
        result = self.handler.resolve_login_wall(self.page, self.ctx)
        assert result is False

    def test_handle_verification_code_returns_none(self):
        result = self.handler.handle_verification_code(self.page, self.ctx)
        assert result is None

    def test_on_submit_clicked_returns_none(self):
        result = self.handler.on_submit_clicked(self.page, self.ctx)
        assert result is None

    def test_detect_success_returns_false(self):
        result = self.handler.detect_success(self.page, self.ctx)
        assert result is False

    # Q2 hooks

    def test_q2_pre_flight_returns_none(self):
        result = self.handler.q2_pre_flight(self.page, self.ctx)
        assert result is None

    def test_q2_resolve_login_wall_returns_false(self):
        result = self.handler.q2_resolve_login_wall(self.page, self.ctx)
        assert result is False


from ats_handlers import get_handler  # noqa: E402


class TestHandlerRegistry:
    def test_unknown_url_returns_default(self):
        handler = get_handler("https://some-random-site.com/apply")
        assert isinstance(handler, DefaultHandler)
        assert handler.platform_name == "unknown"

    def test_workday_url(self):
        handler = get_handler("https://company.wd5.myworkdayjobs.com/en-US/External/job/Senior-SRE")
        assert handler.platform_name == "Workday"

    def test_greenhouse_url(self):
        handler = get_handler("https://boards.greenhouse.io/company/jobs/123")
        assert handler.platform_name == "Greenhouse"

    def test_smartrecruiters_url(self):
        handler = get_handler("https://jobs.smartrecruiters.com/Company/12345")
        assert handler.platform_name == "SmartRecruiters"

    def test_lever_url(self):
        handler = get_handler("https://jobs.lever.co/company/abc-123")
        assert handler.platform_name == "Lever"

    def test_ashby_url(self):
        handler = get_handler("https://jobs.ashbyhq.com/company/abc-123")
        assert handler.platform_name == "Ashby"

    def test_handler_is_singleton(self):
        h1 = get_handler("https://boards.greenhouse.io/a/jobs/1")
        h2 = get_handler("https://boards.greenhouse.io/b/jobs/2")
        assert h1 is h2

    def test_all_hooks_callable_on_default(self):
        handler = get_handler("https://unknown.com/apply")
        page = object()
        ctx = {}
        assert handler.pre_flight(page, ctx) is None
        assert handler.on_step_start(page, ctx) is None
        assert handler.resolve_login_wall(page, ctx) is False
        assert handler.handle_verification_code(page, ctx) is None
        assert handler.on_submit_clicked(page, ctx) is None
        assert handler.detect_success(page, ctx) is False
        assert handler.q2_pre_flight(page, ctx) is None
        assert handler.q2_resolve_login_wall(page, ctx) is False


class TestQ1HandlerIntegration:
    """Verify handler hooks are wired into _navigate_external_form."""

    def _make_page(self, url="https://test.com/apply"):
        """Create a minimal mock page that won't trip up _navigate_external_form."""
        page = MagicMock()
        page.url = url
        page.query_selector_all.return_value = []
        page.query_selector.return_value = None
        page.frames = [page.main_frame]
        page.content.return_value = "<html><body>Test</body></html>"
        page.evaluate.return_value = ""
        return page

    def test_get_handler_called_in_submit_external_apply(self):
        """submit_external_apply calls get_handler to obtain a platform handler."""
        with patch("jobapply.external.get_handler") as mock_gh:
            mock_handler = MagicMock()
            mock_handler.pre_flight.return_value = "failed: test abort"
            mock_gh.return_value = mock_handler

            page = self._make_page()
            context = MagicMock()
            context.pages = [page]

            with (
                patch("jobapply.external._stealth_playwright"),
                patch("jobapply.external._playwright_context") as mock_ctx,
                patch("jobapply.external._ensure_logged_in"),
                patch("jobapply.external._wait_and_dismiss_cookies"),
            ):
                mock_ctx.return_value = (MagicMock(), context, page, True)
                from job_search_apply import submit_external_apply

                result = submit_external_apply(
                    {"id": "t1", "title": "T", "company": "C", "url": "https://test.com/apply"},
                    MagicMock(proxy_rules={}),
                    dry_run=True,
                )

            mock_gh.assert_called_once()
            assert result == "failed: test abort"

    def test_on_step_start_skip_step(self):
        """on_step_start can set skip_step to skip the current iteration."""
        from job_search_apply import _navigate_external_form

        call_count = {"steps": 0}

        class SkipHandler(BaseATSHandler):
            @property
            def platform_name(self):
                return "TestSkip"

            def on_step_start(self, page, ctx):
                call_count["steps"] += 1
                if call_count["steps"] <= 2:
                    ctx["skip_step"] = True
                    return None
                return "failed: done after 3 steps"

        page = self._make_page()
        handler = SkipHandler()
        ctx = {"profile": MagicMock(), "job": {}}

        result = _navigate_external_form(
            page,
            MagicMock(),
            {"id": "t1", "title": "T", "company": "C"},
            "",
            True,
            MagicMock(),
            dry_run=True,
            handler=handler,
            handler_ctx=ctx,
        )
        assert result == "failed: done after 3 steps"
        assert call_count["steps"] == 3

    def test_detect_success_handler_overrides(self):
        """handler.detect_success returning True causes 'submitted' before generic check."""
        from job_search_apply import _navigate_external_form

        class SuccessHandler(BaseATSHandler):
            @property
            def platform_name(self):
                return "TestSuccess"

            def detect_success(self, page, ctx):
                return True

        page = self._make_page()
        page.evaluate.return_value = ""
        handler = SuccessHandler()
        ctx = {"profile": MagicMock(), "job": {}}

        with patch("jobapply.external._extract_page_snapshot", return_value="snapshot"):
            result = _navigate_external_form(
                page,
                MagicMock(),
                {"id": "t1", "title": "T", "company": "C"},
                "",
                True,
                MagicMock(),
                dry_run=True,
                handler=handler,
                handler_ctx=ctx,
            )
        assert result == "submitted"

    def test_resolve_login_wall_handler_continues(self):
        """When handler.resolve_login_wall returns True, loop continues past login page."""
        from job_search_apply import _navigate_external_form

        call_count = {"login_calls": 0, "step_calls": 0}

        class LoginResolver(BaseATSHandler):
            @property
            def platform_name(self):
                return "TestLogin"

            def resolve_login_wall(self, page, ctx):
                call_count["login_calls"] += 1
                return True

            def on_step_start(self, page, ctx):
                call_count["step_calls"] += 1
                if call_count["step_calls"] >= 2:
                    return "failed: enough steps"
                return None

        page = self._make_page()
        handler = LoginResolver()
        ctx = {"profile": MagicMock(), "job": {}}

        login_classification = {
            "page_type": "login",
            "has_required_login": True,
            "has_form_fields": False,
            "has_file_upload": False,
            "notes": "login page",
        }

        with (
            patch("jobapply.external._extract_page_snapshot", return_value="snapshot"),
            patch("jobapply.external._detect_success_or_confirmation", return_value=False),
            patch("jobapply.external._classify_page", return_value=login_classification),
            patch("jobapply.external._find_navigation_button", return_value=("none", None)),
            patch("jobapply.external._answer_external_screening_questions", return_value=0),
            patch("jobapply.external._detect_login_page", return_value=False),
            patch("jobapply.external._detect_captcha", return_value=None),
        ):
            result = _navigate_external_form(
                page,
                MagicMock(),
                {"id": "t1", "title": "T", "company": "C"},
                "",
                True,
                MagicMock(),
                dry_run=False,
                handler=handler,
                handler_ctx=ctx,
            )
        # Handler resolved the login wall, so the loop continued to step 2
        assert call_count["login_calls"] >= 1
        assert result == "failed: enough steps"

    def test_on_submit_clicked_handler_returns_submitted(self):
        """on_submit_clicked returning 'submitted' saves session and returns."""
        from job_search_apply import _navigate_external_form

        class SubmitHandler(BaseATSHandler):
            @property
            def platform_name(self):
                return "TestSubmit"

            def on_submit_clicked(self, page, ctx):
                return "submitted"

        page = self._make_page()
        handler = SubmitHandler()
        ctx = {"profile": MagicMock(), "job": {}}
        mock_context = MagicMock()

        form_classification = {
            "page_type": "form",
            "has_form_fields": True,
            "has_file_upload": False,
            "has_required_login": False,
            "notes": "form page",
        }

        with (
            patch("jobapply.external._extract_page_snapshot", return_value="snapshot"),
            patch("jobapply.external._detect_success_or_confirmation", return_value=False),
            patch("jobapply.external._classify_page", return_value=form_classification),
            patch(
                "jobapply.external._find_navigation_button", return_value=("submit", MagicMock())
            ),
            patch("jobapply.external._answer_external_screening_questions", return_value=0),
            patch("jobapply.external._detect_login_page", return_value=False),
            patch("jobapply.external._detect_captcha", return_value=None),
            patch("jobapply.external._safe_click"),
            patch("jobapply.external._save_session") as mock_save,
        ):
            result = _navigate_external_form(
                page,
                MagicMock(),
                {"id": "t1", "title": "T", "company": "C"},
                "",
                True,
                mock_context,
                dry_run=False,
                handler=handler,
                handler_ctx=ctx,
            )
        assert result == "submitted"
        mock_save.assert_called_once_with(mock_context)

    def test_default_handler_no_behavior_change(self):
        """With DefaultHandler (all no-ops), behavior is identical to no handler."""
        handler = DefaultHandler()
        page = MagicMock()
        ctx = {"profile": MagicMock(), "job": {}}

        # All hooks return their no-op defaults
        assert handler.pre_flight(page, ctx) is None
        assert handler.on_step_start(page, ctx) is None
        assert handler.resolve_login_wall(page, ctx) is False
        assert handler.handle_verification_code(page, ctx) is None
        assert handler.on_submit_clicked(page, ctx) is None
        assert handler.detect_success(page, ctx) is False


class TestQ2HandlerHooks:
    """Verify Q2 handler hooks have correct signatures and behavior."""

    def test_q2_resolve_login_wall_continues_loop(self):
        """When handler.q2_resolve_login_wall returns True, loop continues."""

        class ResolveHandler(BaseATSHandler):
            @property
            def platform_name(self):
                return "TestResolve"

            def q2_resolve_login_wall(self, page, ctx):
                return True

        handler = ResolveHandler()
        assert handler.q2_resolve_login_wall(MagicMock(), {}) is True

    def test_q2_pre_flight_abort_returns_status(self):
        """When q2_pre_flight returns a non-None string, it signals abort."""

        class AbortHandler(BaseATSHandler):
            @property
            def platform_name(self):
                return "TestAbort"

            def q2_pre_flight(self, page, ctx):
                return "failed: pre-flight check failed"

        handler = AbortHandler()
        result = handler.q2_pre_flight(MagicMock(), {})
        assert result == "failed: pre-flight check failed"

    def test_q2_hooks_wired_into_run_page_loop(self):
        """_run_page_loop accepts handler and handler_ctx keyword parameters."""
        import inspect  # noqa: PLC0415

        from assisted_apply_mcp import _run_page_loop  # noqa: PLC0415

        sig = inspect.signature(_run_page_loop)
        params = sig.parameters
        assert "handler" in params
        assert "handler_ctx" in params
        assert params["handler"].default is None
        assert params["handler_ctx"].default is None

    def test_q2_resolve_login_wall_called_before_default_login_wall(self):
        """handler.q2_resolve_login_wall is called during the page loop."""
        from unittest.mock import patch  # noqa: PLC0415

        from assisted_apply_mcp import _run_page_loop  # noqa: PLC0415

        call_order = []

        class TrackingHandler(BaseATSHandler):
            @property
            def platform_name(self):
                return "TrackingHandler"

            def q2_resolve_login_wall(self, page, ctx):
                call_order.append("q2_resolve_login_wall")
                return False

        page = MagicMock()
        page.url = "https://test.example.com/apply"

        handler = TrackingHandler()
        handler_ctx = {"profile": MagicMock(), "job_id": "test_id"}

        with (
            patch("q2apply.loop._detect_submission_success", return_value=False),
            patch("q2apply.loop._detect_email_verification", return_value=False),
            patch("q2apply.loop._detect_rejection", return_value=None),
            patch("q2apply.loop._handle_captcha", return_value=False),
            patch("q2apply.loop._handle_login_wall", return_value=False),
            patch("q2apply.loop._get_page_text_snapshot", return_value="snapshot"),
            patch("q2apply.loop._fix_corrupted_fields"),
            patch("q2apply.loop._ai_analyze_page", return_value=None),
            patch("q2apply.loop._handle_no_actions", return_value="failed: no actions"),
            patch("q2apply.loop._dismiss_cookie_banner"),
            patch("q2apply.loop._clear_errored_uploads"),
        ):
            _run_page_loop(
                page,
                MagicMock(),
                "Title",
                "Company",
                "",
                "",
                "job_id",
                MagicMock(),
                handler=handler,
                handler_ctx=handler_ctx,
            )

        assert "q2_resolve_login_wall" in call_order


from ats_handlers.workday import (  # noqa: E402
    _WD_MAX_ERROR_RELOADS,
    _WD_MAX_VISION_PASSES,
    WD_REVIEW_PARKED_STATUS,
    WorkdayHandler,
)


class TestWorkdayHandler:
    def test_platform_name(self):
        assert WorkdayHandler().platform_name == "Workday"

    def test_pre_flight_dismisses_cookie_banner(self):
        handler = WorkdayHandler()
        page = MagicMock()
        cookie_btn = MagicMock()
        cookie_btn.is_visible.return_value = True
        page.query_selector.return_value = cookie_btn
        ctx = {}
        with patch("job_search_apply._safe_click"):
            result = handler.pre_flight(page, ctx)
        assert result is None

    def test_on_step_start_autofill_popup(self):
        handler = WorkdayHandler()
        page = MagicMock()
        autofill = MagicMock()
        autofill.is_visible.return_value = True
        page.query_selector.side_effect = lambda sel: (
            autofill if "autofillWithResume" in sel else None
        )
        ctx = {}
        with patch("job_search_apply._safe_click"):
            result = handler.on_step_start(page, ctx)
        assert result is None
        assert ctx.get("skip_step") is True

    def test_on_step_start_no_popup(self):
        handler = WorkdayHandler()
        page = MagicMock()
        page.query_selector.return_value = None
        page.evaluate.return_value = False
        ctx = {}
        result = handler.on_step_start(page, ctx)
        assert result is None
        assert "skip_step" not in ctx

    def test_resolve_login_wall_returns_true(self):
        handler = WorkdayHandler()
        assert handler.resolve_login_wall(MagicMock(), {}) is True

    def test_q2_resolve_login_wall_returns_true(self):
        handler = WorkdayHandler()
        assert handler.q2_resolve_login_wall(MagicMock(), {}) is True


from ats_handlers.ashby import AshbyHandler  # noqa: E402
from ats_handlers.lever import LeverHandler  # noqa: E402


class TestLeverHandler:
    """Registry + default-hook checks. Behavioral tests are in
    TestLeverHandlerBehavior at the end of this file (added 2026-07-08 when
    the handler stopped being a passthrough)."""

    def test_platform_name(self):
        assert LeverHandler().platform_name == "Lever"

    def test_unused_hooks_keep_defaults(self):
        """Hooks Lever doesn't implement still return base defaults."""
        handler = LeverHandler()
        page = MagicMock()
        page.url = "https://example.com/careers"
        ctx = {}
        assert handler.resolve_login_wall(page, ctx) is False
        assert handler.handle_verification_code(page, ctx) is None
        assert handler.q2_resolve_login_wall(page, ctx) is False


class TestAshbyHandler:
    def test_platform_name(self):
        assert AshbyHandler().platform_name == "Ashby"

    def test_on_step_start_marks_autofill_upload_to_be_skipped(self):
        """The resume must reach the required Resume field, not the autofill pane.

        Regression driver (2026-09-23): the generic uploader walks file inputs in
        DOM order. Ashby's "Autofill from resume" input comes first with no
        label or id, so it got the resume as an "inferred" upload, which marked
        the resume done; the real required input (#_systemfield_resume) was then
        skipped. The form sat on "Parsing your resume..." with Resume* empty and
        Submit disabled until the step budget ran out.
        """
        handler = AshbyHandler()
        page = MagicMock()
        page.evaluate.return_value = ""
        handler.on_step_start(page, {})
        scripts = [c.args[0] for c in page.evaluate.call_args_list if c.args]
        assert any(
            "ashby-application-form-autofill" in js and "data-jobapply-skip" in js for js in scripts
        )

    def test_on_submit_detects_spam_filter(self):
        handler = AshbyHandler()
        page = MagicMock()
        page.evaluate.return_value = (
            "your application has been flagged as possible spam and could not be submitted"
        )
        ctx = {"job": {"id": "test"}}
        result = handler.on_submit_clicked(page, ctx)
        assert result is not None
        assert "spam" in result.lower()

    def test_on_submit_no_spam_returns_none(self):
        handler = AshbyHandler()
        page = MagicMock()
        page.evaluate.return_value = "thank you for applying"
        ctx = {}
        result = handler.on_submit_clicked(page, ctx)
        assert result is None

    def test_on_submit_handles_exception(self):
        handler = AshbyHandler()
        page = MagicMock()
        page.evaluate.side_effect = Exception("page crashed")
        ctx = {}
        result = handler.on_submit_clicked(page, ctx)
        assert result is None

    def test_pre_flight_detects_spam_banner_on_load(self):
        """Spam banner appearing before submit (flagged from prior attempts)."""
        handler = AshbyHandler()
        page = MagicMock()
        page.evaluate.return_value = (
            "we couldn't submit your application. "
            "your application submission was flagged as possible spam."
        )
        ctx = {"job": {"id": "test"}}
        result = handler.pre_flight(page, ctx)
        assert result is not None
        assert "spam" in result.lower()

    def test_pre_flight_no_spam_returns_none(self):
        handler = AshbyHandler()
        page = MagicMock()
        page.evaluate.return_value = "application form - please fill out"
        ctx = {}
        result = handler.pre_flight(page, ctx)
        assert result is None

    def test_on_step_start_detects_spam_after_hydration(self):
        """React SPA renders the spam banner after hydration, so per-step
        checks catch it even when pre_flight ran too early."""
        handler = AshbyHandler()
        page = MagicMock()
        page.evaluate.return_value = (
            "sre / devops engineer. we couldn't submit your application. "
            "your application submission was flagged as possible spam."
        )
        ctx = {"job": {"id": "test"}}
        result = handler.on_step_start(page, ctx)
        assert result is not None
        assert "spam" in result.lower()

    def test_on_step_start_no_spam_returns_none(self):
        handler = AshbyHandler()
        page = MagicMock()
        page.evaluate.return_value = "first name last name email phone"
        ctx = {}
        result = handler.on_step_start(page, ctx)
        assert result is None


from ats_handlers.greenhouse import GreenhouseHandler  # noqa: E402


class TestGreenhouseHandler:
    def test_platform_name(self):
        assert GreenhouseHandler().platform_name == "Greenhouse"

    def test_pre_flight_dismisses_cookie_banner(self):
        """Coalition (embedded Greenhouse via ?gh_jid=) failed because OneTrust
        overlay blocked Apply-button clicks. pre_flight now accepts the banner."""
        handler = GreenhouseHandler()
        page = MagicMock()
        banner_btn = MagicMock()
        banner_btn.is_visible.return_value = True
        page.query_selector.return_value = banner_btn
        with patch("job_search_apply._safe_click") as safe_click:
            result = handler.pre_flight(page, {})
        assert result is None
        safe_click.assert_called_once()

    def test_pre_flight_noop_without_banner(self):
        handler = GreenhouseHandler()
        page = MagicMock()
        page.query_selector.return_value = None
        with patch("job_search_apply._safe_click") as safe_click:
            handler.pre_flight(page, {})
        safe_click.assert_not_called()

    def test_handle_verification_no_gmail_password(self):
        """Falls through to generic handling when no gmail password."""
        handler = GreenhouseHandler()
        profile = MagicMock()
        profile.gmail_app_password = None
        ctx = {"profile": profile}
        result = handler.handle_verification_code(MagicMock(), ctx)
        assert result is None

    def test_handle_verification_code_found_and_submitted(self):
        handler = GreenhouseHandler()
        page = MagicMock()
        profile = MagicMock()
        profile.gmail_app_password = "test_pw"
        profile.email = "test@test.com"
        ctx = {"profile": profile}

        code_input = MagicMock()
        code_input.is_visible.return_value = True
        label_loc = MagicMock()
        label_loc.count.return_value = 1
        label_loc.first = code_input
        page.get_by_label.return_value = label_loc

        with (
            patch("job_search_apply._fetch_verification_code_from_gmail", return_value="123456"),
            patch("job_search_apply._detect_success_or_confirmation", return_value=True),
            patch("job_search_apply._extract_page_snapshot", return_value=""),
            patch("job_search_apply._safe_click"),
            patch("job_search_apply._find_navigation_button", return_value=(None, None)),
        ):
            result = handler.handle_verification_code(page, ctx)
        assert result == "submitted"

    def test_handle_verification_code_not_found(self):
        handler = GreenhouseHandler()
        page = MagicMock()
        profile = MagicMock()
        profile.gmail_app_password = "test_pw"
        profile.email = "test@test.com"
        ctx = {"profile": profile}

        with patch("job_search_apply._fetch_verification_code_from_gmail", return_value=None):
            result = handler.handle_verification_code(page, ctx)
        assert "not received" in result

    def test_handle_verification_code_rejected(self):
        handler = GreenhouseHandler()
        page = MagicMock()
        profile = MagicMock()
        profile.gmail_app_password = "test_pw"
        profile.email = "test@test.com"
        ctx = {"profile": profile}

        code_input = MagicMock()
        code_input.is_visible.return_value = True
        label_loc = MagicMock()
        label_loc.count.return_value = 1
        label_loc.first = code_input
        page.get_by_label.return_value = label_loc

        with (
            patch("job_search_apply._fetch_verification_code_from_gmail", return_value="123456"),
            patch("job_search_apply._detect_success_or_confirmation", return_value=False),
            patch("job_search_apply._extract_page_snapshot", return_value=""),
            patch("job_search_apply._safe_click"),
            patch("job_search_apply._find_navigation_button", return_value=(None, None)),
        ):
            result = handler.handle_verification_code(page, ctx)
        assert "rejected" in result


from ats_handlers.smartrecruiters import SmartRecruitersHandler  # noqa: E402


class TestSmartRecruitersHandler:
    def test_platform_name(self):
        assert SmartRecruitersHandler().platform_name == "SmartRecruiters"

    def test_pre_flight_navigates_to_oneclick(self):
        handler = SmartRecruitersHandler()
        page = MagicMock()
        page.url = "https://jobs.smartrecruiters.com/Company/12345"
        sr_link = MagicMock()
        sr_link.get_attribute.return_value = (
            "https://jobs.smartrecruiters.com/Company/12345/oneclick-ui/apply"
        )
        page.query_selector.return_value = sr_link
        page.content.return_value = "<html>normal content</html>"
        ctx = {"profile": MagicMock(), "job": {}, "cover_letter_path": ""}
        result = handler.pre_flight(page, ctx)
        assert result is None
        page.goto.assert_called_once()

    def test_pre_flight_datadome_blocks_after_retry(self):
        handler = SmartRecruitersHandler()
        page = MagicMock()
        page.url = "https://jobs.smartrecruiters.com/Company/12345/oneclick-ui/apply"
        page.query_selector.return_value = None
        page.content.return_value = "<html>captcha-delivery verification required</html>"
        ctx = {"profile": MagicMock(), "job": {"id": "test"}, "cover_letter_path": ""}
        with patch("job_search_apply._dump_form_debug"):
            result = handler.pre_flight(page, ctx)
        assert result is not None
        assert "anti-bot" in result or "DataDome" in result

    def test_pre_flight_datadome_clears_after_retry(self):
        handler = SmartRecruitersHandler()
        page = MagicMock()
        page.url = "https://jobs.smartrecruiters.com/Company/12345/oneclick-ui/apply"
        page.query_selector.return_value = None
        # First content() call: has DataDome. After reload: clean
        page.content.side_effect = [
            "<html>captcha-delivery</html>",
            "<html>normal form content</html>",
        ]
        ctx = {"profile": MagicMock(), "job": {}, "cover_letter_path": ""}
        result = handler.pre_flight(page, ctx)
        assert result is None  # DataDome cleared, continues

    def test_pre_flight_already_on_oneclick(self):
        handler = SmartRecruitersHandler()
        page = MagicMock()
        page.url = "https://jobs.smartrecruiters.com/Company/12345/oneclick-ui/apply"
        page.query_selector.return_value = None
        page.content.return_value = "<html>normal</html>"
        ctx = {}
        result = handler.pre_flight(page, ctx)
        assert result is None
        page.goto.assert_not_called()  # already on oneclick, no navigation


class TestWorkdayErrorPageRecovery:
    """Workday's SPA intermittently renders "Something went wrong".

    Regression (2026-08-24, reproduced 2026-08-25): a 0.92-scoring Rolls-Royce
    req failed the batch twice with "no Next/Submit button found". The debug
    SCREENSHOT showed the candidate logged in and the application open on the
    My Information step, with Workday's own error panel telling the user to
    refresh. The HTML dump was misleading -- truncated at 50k with a stale SPA
    <title> -- so the failure looked like a login/navigation problem when it was
    really an unhandled error state. The handler now does what the panel asks.
    """

    def _page_with_text(self, text):
        page = MagicMock()
        page.evaluate.return_value = text.lower()
        return page

    def test_detects_error_panel(self):
        page = self._page_with_text("Something went wrong. Please refresh the page and try again.")
        assert WorkdayHandler._is_error_page(page) is True

    def test_ignores_normal_page(self):
        page = self._page_with_text("My Information")
        assert WorkdayHandler._is_error_page(page) is False

    def test_evaluate_failure_is_not_an_error_page(self):
        page = MagicMock()
        page.evaluate.side_effect = RuntimeError("detached frame")
        assert WorkdayHandler._is_error_page(page) is False

    def test_reloads_on_error_panel(self):
        handler = WorkdayHandler()
        page = self._page_with_text("Something went wrong. Please refresh the page.")
        ctx = {}
        with patch.object(WorkdayHandler, "_dismiss_cookie_banner"):
            assert handler._recover_from_error_page(page, ctx) is True
        page.reload.assert_called_once()
        assert ctx["_wd_error_reloads"] == 1

    def test_reload_is_capped(self):
        """A genuinely broken application must still fail, not loop forever."""
        handler = WorkdayHandler()
        page = self._page_with_text("Something went wrong. Please refresh the page.")
        ctx = {"_wd_error_reloads": _WD_MAX_ERROR_RELOADS}
        with patch.object(WorkdayHandler, "_dismiss_cookie_banner"):
            assert handler._recover_from_error_page(page, ctx) is False
        page.reload.assert_not_called()

    def test_no_reload_on_healthy_page(self):
        handler = WorkdayHandler()
        page = self._page_with_text("My Information")
        assert handler._recover_from_error_page(page, {}) is False
        page.reload.assert_not_called()

    def test_on_step_start_skips_step_after_recovery(self):
        """The reloaded page must not be filled in the same iteration."""
        handler = WorkdayHandler()
        page = MagicMock()
        page.query_selector.return_value = None
        ctx = {"_wd_autofilled": True}
        with (
            patch.object(WorkdayHandler, "_close_blocking_modal"),
            patch.object(WorkdayHandler, "_dismiss_cookie_banner"),
            patch.object(WorkdayHandler, "_recover_from_error_page", return_value=True),
        ):
            assert handler.on_step_start(page, ctx) is None
        assert ctx["skip_step"] is True


class TestWorkdayAccountCreation:
    """Tests for Workday-specific account creation in WorkdayHandler."""

    def test_resolve_login_wall_not_real_login_page(self):
        """Pages without 'create account' text should return True (not a real wall)."""
        handler = WorkdayHandler()
        page = MagicMock()
        page.evaluate.return_value = "some form fields here"
        ctx = {"profile": MagicMock()}
        assert handler.resolve_login_wall(page, ctx) is True

    def test_resolve_login_wall_real_page_no_profile(self):
        """Real login page but no profile should return False."""
        handler = WorkdayHandler()
        page = MagicMock()
        page.evaluate.return_value = "create account sign in already have"
        ctx = {}
        assert handler.resolve_login_wall(page, ctx) is False

    def test_resolve_login_wall_uses_stored_creds(self):
        """Should try stored credentials before account creation."""
        handler = WorkdayHandler()
        page = MagicMock()
        page.evaluate.return_value = "create account already have an account"
        page.url = "https://company.wd5.myworkdayjobs.com/login"
        profile = MagicMock()
        profile.auto_create_accounts = True
        ctx = {"profile": profile}
        with patch("job_search_apply._attempt_ats_login", return_value=True) as mock_login:
            result = handler.resolve_login_wall(page, ctx)
        assert result is True
        mock_login.assert_called_once()

    def test_resolve_login_wall_creates_account(self):
        """When no stored creds, should attempt Workday account creation."""
        handler = WorkdayHandler()
        page = MagicMock()
        page.evaluate.side_effect = [
            "create account sign in already have",  # body text check
        ]
        page.url = "https://company.wd5.myworkdayjobs.com/login"
        profile = MagicMock()
        profile.auto_create_accounts = True
        ctx = {"profile": profile}
        with (
            patch("job_search_apply._attempt_ats_login", return_value=False),
            patch.object(handler, "_create_workday_account", return_value=True) as mock_create,
        ):
            result = handler.resolve_login_wall(page, ctx)
        assert result is True
        mock_create.assert_called_once()

    def test_resolve_login_wall_no_auto_create(self):
        """Without auto_create_accounts, should not attempt account creation."""
        handler = WorkdayHandler()
        page = MagicMock()
        page.evaluate.return_value = "create account sign in already have"
        page.url = "https://company.wd5.myworkdayjobs.com/login"
        profile = MagicMock()
        profile.auto_create_accounts = False
        ctx = {"profile": profile}
        with patch("job_search_apply._attempt_ats_login", return_value=False):
            result = handler.resolve_login_wall(page, ctx)
        assert result is False

    def test_click_submit_button_data_automation_id(self):
        """Should try data-automation-id selectors first."""
        btn = MagicMock()
        btn.is_visible.return_value = True
        page = MagicMock()
        page.query_selector.return_value = btn
        assert WorkdayHandler._click_submit_button(page) is True
        btn.click.assert_called_once()

    def test_click_submit_button_js_fallback(self):
        """Should fall back to JS text matching if no data-automation-id."""
        page = MagicMock()
        page.query_selector.return_value = None
        page.evaluate.return_value = "Create Account"
        assert WorkdayHandler._click_submit_button(page) is True

    def test_close_blocking_modal_runs_evaluate(self):
        """on_step_start should call _close_blocking_modal which runs JS that
        finds known Workday modal heading IDs and clicks the close button."""
        handler = WorkdayHandler()
        page = MagicMock()
        page.evaluate.return_value = "changeEmailModal"
        page.query_selector.return_value = None  # short-circuit autofillWithResume etc.
        # Should run without raising
        handler.on_step_start(page, {})
        # First call to evaluate is the close-modal JS
        assert page.evaluate.called

    def test_close_blocking_modal_no_op_when_no_modal(self):
        page = MagicMock()
        page.evaluate.return_value = None
        WorkdayHandler._close_blocking_modal(page)
        # Should not raise; wait_for_timeout NOT called when nothing closed
        page.wait_for_timeout.assert_not_called()


from ats_handlers.paylocity import PaylocityHandler  # noqa: E402


class TestPaylocityHandler:
    def test_platform_name(self):
        assert PaylocityHandler().platform_name == "Paylocity"

    def test_pre_flight_uploads_resume_to_force_modal(self):
        """Paylocity gates apply on a forceUploadResumeModal whose visible
        button only opens an OS picker. Direct set_input_files on the hidden
        <input type=file> dismisses the modal AND triggers resume autofill."""
        handler = PaylocityHandler()
        page = MagicMock()
        modal = MagicMock()
        modal.is_visible.return_value = True
        file_input = MagicMock()

        def query(sel):
            if sel == "#forceUploadResumeModal":
                return modal
            if sel == "#forceUploadResumeModal input[type='file']":
                return file_input
            return None

        page.query_selector.side_effect = query
        profile = MagicMock()
        profile.resume_path = "/tmp/resume.docx"
        ctx = {"profile": profile}

        result = handler.pre_flight(page, ctx)
        assert result is None
        file_input.set_input_files.assert_called_once_with("/tmp/resume.docx")

    def test_pre_flight_skips_when_no_modal(self):
        handler = PaylocityHandler()
        page = MagicMock()
        page.query_selector.return_value = None
        profile = MagicMock()
        profile.resume_path = "/tmp/resume.docx"
        result = handler.pre_flight(page, {"profile": profile})
        assert result is None

    def test_pre_flight_skips_when_no_resume(self):
        handler = PaylocityHandler()
        page = MagicMock()
        profile = MagicMock()
        profile.resume_path = None
        # Should not even query DOM; return early
        result = handler.pre_flight(page, {"profile": profile})
        assert result is None
        page.query_selector.assert_not_called()


from ats_handlers.workable import WorkableHandler  # noqa: E402


class TestWorkableHandler:
    def test_platform_name(self):
        assert WorkableHandler().platform_name == "Workable"

    def test_pre_flight_dismisses_cookie_banner(self):
        """Workable's cookie-consent renders an aria-modal dialog with a
        backdrop that intercepts pointer events, silently swallowing Submit
        clicks. pre_flight must accept the banner."""
        handler = WorkableHandler()
        page = MagicMock()
        accept_btn = MagicMock()
        accept_btn.is_visible.return_value = True
        page.query_selector.return_value = accept_btn
        with patch("job_search_apply._safe_click") as safe_click:
            result = handler.pre_flight(page, {})
        assert result is None
        safe_click.assert_called_once()

    def test_on_step_start_redismisses_banner(self):
        handler = WorkableHandler()
        page = MagicMock()
        accept_btn = MagicMock()
        accept_btn.is_visible.return_value = True
        page.query_selector.return_value = accept_btn
        with patch("job_search_apply._safe_click") as safe_click:
            handler.on_step_start(page, {})
        safe_click.assert_called_once()


class TestGreenhouseEmbedIframeJump:
    """pre_flight short-circuits ?gh_jid= embeds by navigating to the
    job-boards.greenhouse.io iframe src (Coalition, Nintex pattern)."""

    EMBED_SRC = "https://job-boards.greenhouse.io/embed/job_app?for=coalition&token=1234"

    def _make_page(self, url, iframe_src=None):
        """Page with no cookie banner; optionally a greenhouse embed iframe."""
        page = MagicMock()
        page.url = url
        iframe = None
        if iframe_src is not None:
            iframe = MagicMock()
            iframe.get_attribute.return_value = iframe_src

        def query(sel):
            if sel == "iframe[src*='greenhouse.io']":
                return iframe
            return None

        page.query_selector.side_effect = query
        return page

    def test_embed_detected_and_jumped(self):
        handler = GreenhouseHandler()
        page = self._make_page(
            "https://careers.coalitioninc.com/jobs?gh_jid=1234", iframe_src=self.EMBED_SRC
        )
        result = handler.pre_flight(page, {})
        assert result is None
        page.goto.assert_called_once_with(self.EMBED_SRC, timeout=30000)

    def test_no_gh_jid_param_does_nothing(self):
        handler = GreenhouseHandler()
        page = self._make_page("https://careers.example.com/jobs/1234", iframe_src=self.EMBED_SRC)
        result = handler.pre_flight(page, {})
        assert result is None
        page.goto.assert_not_called()

    def test_gh_jid_but_no_iframe_does_nothing(self):
        handler = GreenhouseHandler()
        page = self._make_page("https://careers.example.com/jobs?gh_jid=1234")
        result = handler.pre_flight(page, {})
        assert result is None
        page.goto.assert_not_called()

    def test_direct_greenhouse_url_does_nothing(self):
        """A URL already on greenhouse.io must not re-jump even with gh_jid."""
        handler = GreenhouseHandler()
        page = self._make_page(
            "https://job-boards.greenhouse.io/embed/job_app?for=x&token=1&gh_jid=1234",
            iframe_src=self.EMBED_SRC,
        )
        result = handler.pre_flight(page, {})
        assert result is None
        page.goto.assert_not_called()

    def test_iframe_without_src_does_nothing(self):
        """get_attribute returning an empty src must skip the jump."""
        handler = GreenhouseHandler()
        page = self._make_page("https://careers.example.com/jobs?gh_jid=1234", iframe_src="")
        result = handler.pre_flight(page, {})
        assert result is None
        page.goto.assert_not_called()


class TestWorkdayConsentCheckbox:
    """_check_consent_checkbox tries data-automation-id, then any visible
    unchecked native checkbox, then a JS DOM walk from the consent text."""

    def test_strategy1_data_automation_id(self):
        page = MagicMock()
        wd_cb = MagicMock()
        wd_cb.is_visible.return_value = True
        page.query_selector.return_value = wd_cb
        WorkdayHandler._check_consent_checkbox(page)
        wd_cb.click.assert_called_once()
        # Strategy 1 hit means strategies 2 and 3 never run
        page.query_selector_all.assert_not_called()
        page.evaluate.assert_not_called()

    def test_strategy2_native_checkbox_checked(self):
        page = MagicMock()
        page.query_selector.return_value = None
        cb = MagicMock()
        cb.is_visible.return_value = True
        cb.is_checked.return_value = False
        page.query_selector_all.return_value = [cb]
        WorkdayHandler._check_consent_checkbox(page)
        cb.check.assert_called_once()
        page.evaluate.assert_not_called()

    def test_already_checked_native_checkbox_skipped(self):
        page = MagicMock()
        page.query_selector.return_value = None
        cb = MagicMock()
        cb.is_visible.return_value = True
        cb.is_checked.return_value = True
        page.query_selector_all.return_value = [cb]
        page.evaluate.return_value = None
        WorkdayHandler._check_consent_checkbox(page)
        cb.check.assert_not_called()
        # Falls through to the JS walk since nothing was checked
        page.evaluate.assert_called_once()

    def test_strategy3_js_walk_fallback(self):
        page = MagicMock()
        page.query_selector.return_value = None
        page.query_selector_all.return_value = []
        page.evaluate.return_value = "sibling"
        WorkdayHandler._check_consent_checkbox(page)
        page.evaluate.assert_called_once()
        page.wait_for_timeout.assert_called_once_with(500)

    def test_nothing_found_no_crash(self):
        page = MagicMock()
        page.query_selector.return_value = None
        page.query_selector_all.return_value = []
        page.evaluate.return_value = None
        WorkdayHandler._check_consent_checkbox(page)
        page.wait_for_timeout.assert_not_called()

    def test_page_error_swallowed(self):
        page = MagicMock()
        page.query_selector.side_effect = Exception("page crashed")
        # Must not raise
        WorkdayHandler._check_consent_checkbox(page)


class TestAshbyLocationCombobox:
    """_fill_location_combobox types the profile city into Ashby's
    'Start typing...' combobox and clicks the first visible suggestion."""

    def _make_combobox(self, value=""):
        cb = MagicMock()
        cb.is_visible.return_value = True
        cb.input_value.return_value = value
        return cb

    def test_combobox_found_and_filled(self):
        page = MagicMock()
        cb = self._make_combobox()
        page.query_selector_all.return_value = [cb]
        page.evaluate.return_value = "Austin, Texas, United States"
        AshbyHandler._fill_location_combobox(page, MagicMock(city="Austin"))
        cb.type.assert_called_once_with("Austin", delay=60)
        page.evaluate.assert_called_once()

    def test_combobox_absent_is_noop(self):
        page = MagicMock()
        page.query_selector_all.return_value = []
        AshbyHandler._fill_location_combobox(page, MagicMock(city="Austin"))
        page.evaluate.assert_not_called()

    def test_already_selected_tag_skipped(self):
        page = MagicMock()
        cb = self._make_combobox(value="Indianapolis")
        cb.evaluate.return_value = True  # adjacent tag element exists
        page.query_selector_all.return_value = [cb]
        AshbyHandler._fill_location_combobox(page, MagicMock(city="Indianapolis"))
        cb.click.assert_not_called()
        cb.type.assert_not_called()

    def test_no_suggestion_clears_typed_text(self):
        page = MagicMock()
        cb = self._make_combobox()
        page.query_selector_all.return_value = [cb]
        page.evaluate.return_value = None  # no role=option appeared
        AshbyHandler._fill_location_combobox(page, MagicMock(city="Austin"))
        # fill("") before typing, then fill("") again to clear the stray text
        assert cb.fill.call_count == 2
        assert cb.fill.call_args_list[-1].args == ("",)

    def test_profile_without_city_skips(self):
        """Fixed 2026-07-08: without a profile city the combobox is skipped
        entirely instead of typing a hardcoded default location."""
        from types import SimpleNamespace  # noqa: PLC0415

        page = MagicMock()
        cb = self._make_combobox()
        page.query_selector_all.return_value = [cb]
        AshbyHandler._fill_location_combobox(page, SimpleNamespace(city=None))
        cb.type.assert_not_called()
        page.query_selector_all.assert_not_called()


from datetime import datetime, timedelta  # noqa: E402


class TestAshbyDatePickers:
    """_fill_required_date_pickers opens react-datepicker popups on empty
    required inputs and clicks a date roughly two weeks out."""

    def _make_wrapper(self, input_value=None, required_input=True):
        wrapper = MagicMock()
        inp = MagicMock()
        inp.get_attribute.return_value = input_value
        wrapper.query_selector.return_value = inp if required_input else None
        return wrapper, inp

    def test_required_empty_picker_filled(self):
        page = MagicMock()
        wrapper, inp = self._make_wrapper(input_value=None)
        page.query_selector_all.return_value = [wrapper]

        target = datetime.now() + timedelta(days=14)
        month_label = MagicMock()
        month_label.inner_text.return_value = target.strftime("%B %Y")
        day_btn = MagicMock()
        day_btn.is_visible.return_value = True

        def query(sel):
            if "current-month" in sel:
                return month_label
            if sel.startswith("[aria-label="):
                return day_btn
            return None

        page.query_selector.side_effect = query
        AshbyHandler._fill_required_date_pickers(page)
        inp.click.assert_called_once_with(force=True)
        day_btn.click.assert_called_once()

    def test_no_pickers_is_noop(self):
        page = MagicMock()
        page.query_selector_all.return_value = []
        AshbyHandler._fill_required_date_pickers(page)
        page.query_selector.assert_not_called()

    def test_picker_without_required_input_skipped(self):
        page = MagicMock()
        wrapper, _ = self._make_wrapper(required_input=False)
        page.query_selector_all.return_value = [wrapper]
        AshbyHandler._fill_required_date_pickers(page)
        page.query_selector.assert_not_called()

    def test_already_filled_picker_skipped(self):
        page = MagicMock()
        wrapper, inp = self._make_wrapper(input_value="07/22/2026")
        page.query_selector_all.return_value = [wrapper]
        AshbyHandler._fill_required_date_pickers(page)
        inp.click.assert_not_called()


class TestLeverHandlerBehavior:
    """Behavioral tests for the Lever handler (added 2026-07-08 when it
    stopped being a passthrough). Fake-page style; the handler methods are
    pure DOM manipulation over query_selector(_all)."""

    def test_goto_apply_page_jumps_from_posting(self):
        """A bare posting URL navigates to the /apply form page."""
        page = MagicMock()
        page.url = "https://jobs.lever.co/smarsh/7a3a22dd-09be-4a95-b8e4-45a3f4d534fe"
        LeverHandler._goto_apply_page(page)
        page.goto.assert_called_once()
        assert page.goto.call_args.args[0] == (
            "https://jobs.lever.co/smarsh/7a3a22dd-09be-4a95-b8e4-45a3f4d534fe/apply"
        )

    def test_goto_apply_page_preserves_query(self):
        page = MagicMock()
        page.url = "https://jobs.lever.co/smarsh/7a3a22dd-09be-4a95-b8e4-45a3f4d534fe?src=LinkedIn"
        LeverHandler._goto_apply_page(page)
        assert page.goto.call_args.args[0].endswith("/apply?src=LinkedIn")

    def test_goto_apply_page_handles_eu_domain(self):
        page = MagicMock()
        page.url = "https://jobs.eu.lever.co/company/da18148f-35d7-4d0f-83f8-1acfc8550325"
        LeverHandler._goto_apply_page(page)
        page.goto.assert_called_once()

    def test_goto_apply_page_ignores_non_posting_url(self):
        """A non-posting URL (already on /apply, or unrelated) does not jump."""
        page = MagicMock()
        page.url = "https://jobs.lever.co/smarsh/7a3a22dd-09be-4a95-b8e4-45a3f4d534fe/apply"
        LeverHandler._goto_apply_page(page)
        page.goto.assert_not_called()

    def test_pre_flight_calls_goto(self):
        handler = LeverHandler()
        page = MagicMock()
        page.url = "https://jobs.lever.co/smarsh/7a3a22dd-09be-4a95-b8e4-45a3f4d534fe"
        assert handler.pre_flight(page, {}) is None
        page.goto.assert_called_once()

    def test_detect_success_on_thanks_url(self):
        handler = LeverHandler()
        page = MagicMock()
        page.url = "https://jobs.lever.co/smarsh/abc/thanks"
        assert handler.detect_success(page, {}) is True

    def test_detect_success_false_on_apply_url(self):
        handler = LeverHandler()
        page = MagicMock()
        page.url = "https://jobs.lever.co/smarsh/abc/apply"
        page.evaluate.return_value = "submit application"
        assert handler.detect_success(page, {}) is False

    def test_on_step_start_noop_without_form(self):
        """No #application-form means the fill helpers never run."""
        handler = LeverHandler()
        page = MagicMock()
        page.query_selector.return_value = None  # no form
        handler._relay_hcaptcha_token = MagicMock(return_value=False)
        result = handler.on_step_start(page, {"profile": MagicMock()})
        assert result is None

    def test_strip_required_from_unchecked(self):
        """Unchecked toggles in an answered group lose their required attr."""
        checked = MagicMock()
        checked.is_checked.return_value = True
        unchecked = MagicMock()
        unchecked.is_checked.return_value = False
        LeverHandler._strip_required_from_unchecked([checked, unchecked])
        unchecked.evaluate.assert_called_once()
        checked.evaluate.assert_not_called()

    def test_fill_contact_fields_uses_name_attributes(self):
        """Contact fields fill from stable name selectors, skipping empties."""
        handler = LeverHandler()
        profile = MagicMock()
        profile.full_name = "Christopher Draper"
        profile.email = "chris@example.com"
        profile.phone = "317-555-0100"
        profile.current_employer = ""
        profile.linkedin_url = None
        profile.github_url = None

        filled_inputs = {}

        def qs(selector):
            if "name='name'" in selector:
                m = MagicMock()
                m.is_visible.return_value = True
                m.input_value.return_value = ""
                filled_inputs["name"] = m
                return m
            if "name='email'" in selector:
                m = MagicMock()
                m.is_visible.return_value = True
                m.input_value.return_value = ""
                filled_inputs["email"] = m
                return m
            return None

        page = MagicMock()
        page.query_selector.side_effect = qs
        count = handler._fill_contact_fields(page, profile)
        assert count == 2
        filled_inputs["name"].fill.assert_called_once_with("Christopher Draper")
        filled_inputs["email"].fill.assert_called_once_with("chris@example.com")

    def test_fill_contact_fields_skips_prefilled(self):
        handler = LeverHandler()
        profile = MagicMock()
        profile.full_name = "Christopher Draper"
        profile.email = profile.phone = profile.current_employer = ""
        profile.linkedin_url = profile.github_url = None
        prefilled = MagicMock()
        prefilled.is_visible.return_value = True
        prefilled.input_value.return_value = "Already Here"
        page = MagicMock()
        page.query_selector.side_effect = lambda s: prefilled if "name='name'" in s else None
        assert handler._fill_contact_fields(page, profile) == 0
        prefilled.fill.assert_not_called()


class TestWorkdayReviewDetector:
    def test_parked_status_is_not_a_failure(self):
        # workflow.py only categorizes statuses that start with "failed";
        # parked must never be seen as a failure or queued to Q2.
        assert not WD_REVIEW_PARKED_STATUS.startswith("failed")

    def test_is_review_page_true_when_js_reports_review(self):
        page = MagicMock()
        page.evaluate.return_value = True
        assert WorkdayHandler._is_review_page(page) is True

    def test_is_review_page_false_when_js_reports_false(self):
        page = MagicMock()
        page.evaluate.return_value = False
        assert WorkdayHandler._is_review_page(page) is False

    def test_is_review_page_false_on_evaluate_error(self):
        page = MagicMock()
        page.evaluate.side_effect = RuntimeError("page closed")
        assert WorkdayHandler._is_review_page(page) is False


class TestWorkdayAtTerminalSubmit:
    """FINDING #1: selector-independent Submit-only backstop for the
    Review-stop gate. Must never depend on _is_review_page's unvalidated
    hasSummary selector, and must fail CLOSED (False) on error since it only
    ever ADDS parking, never removes the existing gate."""

    def test_true_when_submit_only_reported(self):
        page = MagicMock()
        page.evaluate.return_value = True
        assert WorkdayHandler._at_terminal_submit(page) is True

    def test_false_when_forward_button_also_present(self):
        page = MagicMock()
        page.evaluate.return_value = False
        assert WorkdayHandler._at_terminal_submit(page) is False

    def test_false_on_evaluate_error(self):
        page = MagicMock()
        page.evaluate.side_effect = RuntimeError("page closed")
        assert WorkdayHandler._at_terminal_submit(page) is False


class TestWorkdayReviewStop:
    def _handler_page(self, is_review):
        handler = WorkdayHandler()
        page = MagicMock()
        # No autofill/manual popups, no blocking modal:
        page.query_selector.return_value = None
        with patch.object(WorkdayHandler, "_is_review_page", return_value=is_review):
            return handler, page

    def test_on_step_start_parks_on_review_page_when_kill_switch_set(self, monkeypatch):
        monkeypatch.setenv("JOBAPPLY_WORKDAY_PARK", "1")
        handler, page = self._handler_page(is_review=True)
        with patch.object(WorkdayHandler, "_is_review_page", return_value=True):
            result = handler.on_step_start(page, {})
        assert result == WD_REVIEW_PARKED_STATUS

    def test_on_step_start_submits_on_review_page_by_default(self, monkeypatch):
        """2026-09-24: the owner switched Workday from park-at-Review to submit."""
        monkeypatch.delenv("JOBAPPLY_WORKDAY_PARK", raising=False)
        handler, page = self._handler_page(is_review=True)
        with (
            patch.object(WorkdayHandler, "_is_review_page", return_value=True),
            patch.object(WorkdayHandler, "_submit_review", return_value="submitted") as sub,
        ):
            result = handler.on_step_start(page, {})
        assert result == "submitted"
        sub.assert_called_once()

    def test_on_step_start_returns_none_off_review_page(self):
        handler = WorkdayHandler()
        page = MagicMock()
        page.query_selector.return_value = None
        with (
            patch.object(WorkdayHandler, "_is_review_page", return_value=False),
            patch.object(WorkdayHandler, "_at_terminal_submit", return_value=False),
            patch.object(WorkdayHandler, "_is_form_page", return_value=False),
        ):
            result = handler.on_step_start(page, {})
        assert result is None

    def test_on_step_start_parks_on_terminal_submit_backstop_alone(self, monkeypatch):
        """FINDING #1: even when _is_review_page's selector guess is wrong
        (returns False), the selector-independent Submit-only backstop must
        still park -- the 'never auto-submit Workday' invariant cannot depend
        on a single unvalidated selector."""
        monkeypatch.setenv("JOBAPPLY_WORKDAY_PARK", "1")
        handler = WorkdayHandler()
        page = MagicMock()
        page.query_selector.return_value = None
        with (
            patch.object(WorkdayHandler, "_is_review_page", return_value=False),
            patch.object(WorkdayHandler, "_at_terminal_submit", return_value=True),
        ):
            result = handler.on_step_start(page, {})
        assert result == WD_REVIEW_PARKED_STATUS

    def test_review_stop_not_reached_when_autofill_popup_present(self):
        # If the Autofill-with-Resume popup is up, that path returns first with
        # skip_step and never calls _is_review_page.
        handler = WorkdayHandler()
        page = MagicMock()
        autofill = MagicMock()
        autofill.is_visible.return_value = True
        page.query_selector.side_effect = lambda sel: (
            autofill if "autofillWithResume" in sel else None
        )
        ctx = {}
        with (
            patch("job_search_apply._safe_click"),
            patch.object(WorkdayHandler, "_is_review_page") as is_review,
        ):
            result = handler.on_step_start(page, ctx)
        assert ctx.get("skip_step") is True
        assert result is None
        is_review.assert_not_called()


class TestWorkdayVisionDrive:
    """The generic deterministic dropdown handler corrupts Workday's searchable
    dropdowns (reads merged option lists, fills garbage, page never
    validates/advances). Fix: drive every Workday form page with vision and
    skip the deterministic fill entirely, bounded by _WD_MAX_VISION_PASSES."""

    def _run(self, page, ctx, is_form_page=True, vcp_kwargs=None):
        with (
            patch.object(WorkdayHandler, "_is_review_page", return_value=False),
            patch.object(WorkdayHandler, "_at_terminal_submit", return_value=False),
            patch.object(WorkdayHandler, "_is_form_page", return_value=is_form_page),
            patch("ats_handlers._workday_vision.vision_complete_page", **(vcp_kwargs or {})) as vcp,
        ):
            result = WorkdayHandler().on_step_start(page, ctx)
            return result, vcp

    def test_form_page_vision_completes_once_and_skips_deterministic_fill(self):
        page = MagicMock()
        page.query_selector.return_value = None
        ctx = {"profile": MagicMock()}
        result, vcp = self._run(page, ctx, is_form_page=True)
        vcp.assert_called_once()
        assert ctx["skip_step"] is True
        assert result is None
        assert ctx["_wd_vpasses"] == 1

    def test_vision_not_called_once_budget_exhausted(self):
        page = MagicMock()
        page.query_selector.return_value = None
        ctx = {"profile": MagicMock(), "_wd_vpasses": _WD_MAX_VISION_PASSES}
        result, vcp = self._run(page, ctx, is_form_page=True)
        vcp.assert_not_called()
        assert result is None

    def test_vision_not_called_on_non_form_page(self):
        page = MagicMock()
        page.query_selector.return_value = None
        ctx = {"profile": MagicMock()}
        result, vcp = self._run(page, ctx, is_form_page=False)
        vcp.assert_not_called()
        assert result is None
        assert "_wd_vpasses" not in ctx

    def test_vision_exception_is_swallowed(self):
        page = MagicMock()
        page.query_selector.return_value = None
        ctx = {"profile": MagicMock()}
        result, vcp = self._run(
            page, ctx, is_form_page=True, vcp_kwargs={"side_effect": RuntimeError("boom")}
        )
        vcp.assert_called_once()
        assert result is None
        assert ctx["skip_step"] is True


from jobapply.external import (  # noqa: E402
    _canonical_ats_key,
    _duplicate_ats_application,
)


class TestAtsRequisitionDedup:
    """One ATS requisition can be fronted by several job-board postings.

    Regression (2026-08-25): LinkedIn lists Rolls-Royce reqs both plainly and
    in a "with verification" variant. Postings 4437870820 and 4456063734 have
    different ids, so batch dedup saw two jobs, but both resolve to the same
    Workday requisition -- which was driven to Review twice in one run, each
    pass costing a vision run (~$1.20, ~3 min) and an application slot.
    """

    WD = "https://rr.wd3.myworkdayjobs.com/en-US/professional/job/Indianapolis/Eng_JR1"

    def test_apply_subpath_collapses_to_the_requisition(self):
        assert _canonical_ats_key(f"{self.WD}/apply/autofillWithResume") == _canonical_ats_key(
            self.WD
        )

    def test_query_and_fragment_ignored(self):
        assert _canonical_ats_key(f"{self.WD}?source=LINKEDIN#top") == _canonical_ats_key(self.WD)

    def test_trailing_slash_ignored(self):
        assert _canonical_ats_key(f"{self.WD}/") == _canonical_ats_key(self.WD)

    def test_distinct_requisitions_do_not_collide(self):
        other = self.WD.replace("Eng_JR1", "Eng_JR2")
        assert _canonical_ats_key(other) != _canonical_ats_key(self.WD)

    def test_second_posting_for_same_req_is_skipped(self):
        log = [{"status": "review_parked: manual submit required", "ats_url": self.WD}]
        with patch("jobapply.applog.load_log", return_value=log):
            result = _duplicate_ats_application(f"{self.WD}/apply/autofillWithResume")
        assert result == "skipped: already applied to this requisition"

    def test_new_requisition_proceeds(self):
        log = [{"status": "submitted", "ats_url": self.WD}]
        with patch("jobapply.applog.load_log", return_value=log):
            assert _duplicate_ats_application(self.WD.replace("Eng_JR1", "Eng_JR9")) is None

    GH = "https://job-boards.greenhouse.io/embed/job_app"

    def test_greenhouse_embed_jobs_do_not_collide(self):
        # 2026-10-03: query stripping made every embed URL one key.
        gemini = f"{self.GH}?for=gemini&token=8099895&gh_jid=8099895&gh_src=x"
        other = f"{self.GH}?for=voxel51&token=4400001"
        assert _canonical_ats_key(gemini) != _canonical_ats_key(other)
        # Tracking params still ignored for the same job.
        assert _canonical_ats_key(gemini) == _canonical_ats_key(
            f"{self.GH}?gh_jid=8099895&token=8099895&for=gemini"
        )

    def test_embed_url_without_a_job_id_is_not_deduped(self):
        assert _canonical_ats_key(f"{self.GH}?for=voxel51&validityToken=abc") == ""
        log = [{"status": "submitted", "ats_url": f"{self.GH}?for=x&validityToken=1"}]
        with patch("jobapply.applog.load_log", return_value=log):
            assert _duplicate_ats_application(f"{self.GH}?for=y&validityToken=2") is None

    def test_unattempted_status_does_not_block(self):
        log = [{"status": "dry_run", "ats_url": self.WD}]
        with patch("jobapply.applog.load_log", return_value=log):
            assert _duplicate_ats_application(self.WD) is None

    def test_blank_and_about_blank_are_ignored(self):
        with patch("jobapply.applog.load_log", return_value=[]):
            assert _duplicate_ats_application("") is None
            assert _duplicate_ats_application("about:blank") is None


from ats_handlers.csod import (  # noqa: E402
    _CSOD_MAX_ADVANCES,
    _CSOD_MAX_PRUNES,
    CornerstoneHandler,
)


class TestCornerstoneHandler:
    """Cornerstone OnDemand (linde.csod.com et al).

    The generic filler cannot see CSOD's contact fields: first/last name and
    email carry an EMPTY aria-label and no <label for>, identified only by
    aria-labelledby="actionItem.firstName.idTag-error". Verified live against
    Linde's Product Engineer II requisition.
    """

    def test_platform_name(self):
        assert CornerstoneHandler().platform_name == "Cornerstone"

    def test_url_resolves_to_handler(self):
        h = get_handler("https://linde.csod.com/ux/ats/careersite/23/requisition/31102/application")
        assert h.platform_name == "Cornerstone"

    def test_careersite_path_alone_resolves(self):
        h = get_handler("https://careers.acme.com/ux/ats/careersite/5/requisition/9/application")
        assert h.platform_name == "Cornerstone"

    def test_confirmation_returns_submitted(self):
        handler = CornerstoneHandler()
        page = MagicMock()
        page.evaluate.return_value = "thank you for applying to linde"
        with patch.object(CornerstoneHandler, "_dismiss_cookie_banner"):
            assert handler.on_step_start(page, {"profile": MagicMock()}) == "submitted"

    def test_detect_success_on_confirmation(self):
        page = MagicMock()
        page.evaluate.return_value = "your application has been submitted"
        assert CornerstoneHandler().detect_success(page, {}) is True

    def test_advance_stops_after_cap(self):
        """A step that never validates must fail, not click Next forever."""
        handler = CornerstoneHandler()
        page = MagicMock()
        page.evaluate.return_value = ""
        ctx = {
            "profile": MagicMock(),
            "_csod_filled": "application",
            "_csod_advances": _CSOD_MAX_ADVANCES,
        }
        with (
            patch.object(CornerstoneHandler, "_dismiss_cookie_banner"),
            patch.object(CornerstoneHandler, "_is_application_form", staticmethod(lambda p: True)),
            patch.object(CornerstoneHandler, "_is_confirmation", staticmethod(lambda p: False)),
        ):
            result = handler.on_step_start(page, ctx)
        assert result is not None and result.startswith("failed:")

    def test_generic_filler_is_bypassed(self):
        """The generic filler guesses on this markup, so it must never run."""
        handler = CornerstoneHandler()
        page = MagicMock()
        ctx = {"profile": MagicMock()}
        with (
            patch.object(CornerstoneHandler, "_dismiss_cookie_banner"),
            patch.object(CornerstoneHandler, "_is_application_form", staticmethod(lambda p: True)),
            patch.object(CornerstoneHandler, "_is_confirmation", staticmethod(lambda p: False)),
            patch.object(CornerstoneHandler, "_fill"),
            patch.object(CornerstoneHandler, "_step_key", staticmethod(lambda p: "Step 1 of 2")),
        ):
            handler.on_step_start(page, ctx)
        assert ctx["skip_step"] is True

    def test_non_form_page_left_to_generic_logic(self):
        handler = CornerstoneHandler()
        page = MagicMock()
        with (
            patch.object(CornerstoneHandler, "_dismiss_cookie_banner"),
            patch.object(CornerstoneHandler, "_is_confirmation", staticmethod(lambda p: False)),
            patch.object(CornerstoneHandler, "_is_application_form", staticmethod(lambda p: False)),
        ):
            assert handler.on_step_start(page, {"profile": MagicMock()}) is None

    # ---- EEO option matching ------------------------------------------------

    GENDER = ["Please Select", "I do not want to answer", "Male", "Female", "Non-binary / Other"]
    ETHNIC = ["Please Select", "I do not want to answer", "Hispanic or Latino", "Asian"]

    def test_exact_profile_answer_wins(self):
        assert CornerstoneHandler._match_option("Male", self.GENDER) == "Male"

    def test_substring_profile_answer_matches(self):
        got = CornerstoneHandler._match_option("Hispanic", self.ETHNIC)
        assert got == "Hispanic or Latino"

    def test_prefer_not_to_say_maps_to_decline(self):
        assert (
            CornerstoneHandler._match_option("Prefer not to say", self.GENDER)
            == "I do not want to answer"
        )

    def test_blank_answer_declines(self):
        assert CornerstoneHandler._match_option("", self.GENDER) == "I do not want to answer"

    def test_placeholder_is_never_selected(self):
        assert CornerstoneHandler._match_option("nonsense value", self.GENDER) != "Please Select"

    def test_no_real_options_yields_nothing(self):
        assert CornerstoneHandler._match_option("Male", ["Please Select"]) == ""


class TestRadioGroupFalsePositive:
    """A <fieldset> of text inputs is not a radio group.

    Regression (2026-08-26): the selector in _answer_radio_buttons required
    :has(input[type='radio']) on every alternative EXCEPT the bare "fieldset",
    so an accessible section wrapper -- <fieldset><legend>Contact
    Information</legend> around First Name / Last Name / Phone -- was treated
    as a question whose options were its field labels. The AI picked one and we
    clicked <label for="firstName">, which merely focuses the input. Zero
    fields filled per step until the stall detector failed the application.
    """

    def test_selector_requires_a_radio_in_every_alternative(self):
        import inspect

        from jobapply.forms import _answer_radio_buttons

        src = inspect.getsource(_answer_radio_buttons)
        selector = src.split("query_selector_all(")[1].split(")")[0]
        for alternative in selector.split(","):
            alternative = alternative.strip().strip('"').strip()
            if not alternative:
                continue
            assert "input[type='radio']" in alternative, f"unguarded alternative: {alternative}"

    def test_container_without_radios_is_skipped(self):
        from jobapply.forms import _answer_radio_buttons

        section = MagicMock()
        section.query_selector.return_value = None  # no radios anywhere
        page = MagicMock()
        page.query_selector_all.return_value = [section]

        _answer_radio_buttons(page, MagicMock())

        # Never asked for the legend, so never posed a question about it.
        for call in section.query_selector.call_args_list:
            assert "legend" not in str(call)


class TestDebugDumpCapturesTheForm:
    """A debug dump must never be just the cookie banner.

    Regression (2026-08-26): _MODAL_SEL's bare [role='dialog'] also matches
    OneTrust's consent widget (#onetrust-pc-sdk), so two Cornerstone dumps
    contained only the Privacy Preference Center and no form markup at all.
    """

    def test_falls_back_to_page_when_modal_has_no_fields(self, tmp_path, monkeypatch):
        import jobapply.forms as forms

        monkeypatch.setattr(forms, "DEBUG_DIR", tmp_path)
        cookie_widget = MagicMock()
        cookie_widget.query_selector.return_value = None  # consent widget: no form fields
        cookie_widget.inner_html.return_value = "<h2>Privacy Preference Center</h2>"
        page = MagicMock()
        page.query_selector.return_value = cookie_widget
        page.content.return_value = "<form><input name='firstName'></form>"

        forms._dump_form_debug(page, "li_1", "stalled")

        dumped = next(tmp_path.glob("*.html")).read_text()
        assert "firstName" in dumped
        assert "Privacy Preference Center" not in dumped

    def test_consent_widget_is_rejected_even_though_it_has_inputs(self, tmp_path, monkeypatch):
        """OneTrust carries category toggles and a search box, so "has fields"
        alone still let the cookie banner win."""
        import jobapply.forms as forms

        monkeypatch.setattr(forms, "DEBUG_DIR", tmp_path)
        widget = MagicMock()
        widget.query_selector.return_value = MagicMock()  # the toggles
        widget.evaluate.return_value = True  # matches a consent selector
        widget.inner_html.return_value = "<h2>Privacy Preference Center</h2>"
        page = MagicMock()
        page.query_selector.return_value = widget
        page.content.return_value = "<form><input name='firstName'></form>"

        forms._dump_form_debug(page, "li_3", "stalled")

        dumped = next(tmp_path.glob("*.html")).read_text()
        assert "firstName" in dumped
        assert "Privacy Preference Center" not in dumped

    def test_real_form_modal_is_still_preferred(self, tmp_path, monkeypatch):
        import jobapply.forms as forms

        monkeypatch.setattr(forms, "DEBUG_DIR", tmp_path)
        modal = MagicMock()
        modal.query_selector.return_value = MagicMock()  # has a field
        modal.evaluate.return_value = False  # not a consent widget
        modal.inner_html.return_value = "<input name='realFormField'>"
        page = MagicMock()
        page.query_selector.return_value = modal
        page.content.return_value = "<html>whole page</html>"

        forms._dump_form_debug(page, "li_2", "stalled")

        dumped = next(tmp_path.glob("*.html")).read_text()
        assert "realFormField" in dumped


class TestCornerstoneResumeBlocks:
    """CSOD's résumé parser leaves required sub-fields empty, blocking the step."""

    def test_degree_mapping(self):
        m = CornerstoneHandler._degree_option
        assert m("B.S. Mechanical Engineering") == "bachelor's"
        assert m("Bachelor of Science") == "bachelor's"
        assert m("M.S. Aerospace") == "master's"
        assert m("PhD") == "doctoral"
        assert m("Associate's") == "associate's"
        assert m("") == ""
        assert m("Certificate in welding") == ""

    def test_prune_stops_when_nothing_incomplete(self):
        page = MagicMock()
        page.evaluate_handle.return_value.as_element.return_value = None
        CornerstoneHandler._prune_incomplete_blocks(page, MagicMock())
        assert page.evaluate_handle.call_count == 1

    def test_prune_deletes_then_rechecks(self):
        page = MagicMock()
        button = MagicMock()
        button.inner_text.return_value = "Delete Education"
        first, second = MagicMock(), MagicMock()
        first.as_element.return_value = button
        second.as_element.return_value = None
        page.evaluate_handle.side_effect = [first, second]
        CornerstoneHandler._prune_incomplete_blocks(page, MagicMock())
        button.click.assert_called_once()

    def test_prune_is_bounded(self):
        """A page that never becomes valid must not loop forever."""
        page = MagicMock()
        button = MagicMock()
        button.inner_text.return_value = "Delete Education"
        handle = MagicMock()
        handle.as_element.return_value = button
        page.evaluate_handle.return_value = handle
        CornerstoneHandler._prune_incomplete_blocks(page, MagicMock())
        assert page.evaluate_handle.call_count <= _CSOD_MAX_PRUNES


class TestCornerstoneDryRunSafety:
    """A dry run must never submit a real application.

    Regression (2026-08-26): the handler drives the form itself and bypasses
    the generic loop via skip_step -- and the generic loop is where dry_run is
    honoured. A --dry-run therefore clicked Submit on Linde's step 2 four
    times; it only failed to go through because validation happened to block
    it. dry_run now reaches the handler through handler_ctx.
    """

    def test_submit_is_not_clicked_when_dry_run(self):
        page = MagicMock()
        page.query_selector_all.return_value = []
        with patch.object(CornerstoneHandler, "_has_submit", staticmethod(lambda p: True)):
            assert CornerstoneHandler._advance(page, allow_submit=False) == "submit_blocked"

    def test_dry_run_stops_before_submit(self):
        handler = CornerstoneHandler()
        page = MagicMock()
        ctx = {"profile": MagicMock(), "dry_run": True, "_csod_filled": "Step 2 of 2"}
        with (
            patch.object(CornerstoneHandler, "_dismiss_cookie_banner"),
            patch.object(CornerstoneHandler, "_is_confirmation", staticmethod(lambda p: False)),
            patch.object(
                CornerstoneHandler, "_is_application_form", classmethod(lambda c, p: True)
            ),
            patch.object(CornerstoneHandler, "_step_key", staticmethod(lambda p: "Step 2 of 2")),
            patch.object(
                CornerstoneHandler,
                "_advance",
                staticmethod(lambda p, allow_submit=True: "submit_blocked"),
            ),
        ):
            assert handler.on_step_start(page, ctx) == "dry_run"

    def test_real_run_is_allowed_to_submit(self):
        handler = CornerstoneHandler()
        page = MagicMock()
        seen = {}

        def fake_advance(p, allow_submit=True):
            seen["allow_submit"] = allow_submit
            return "submit"

        ctx = {"profile": MagicMock(), "dry_run": False, "_csod_filled": "Step 2 of 2"}
        with (
            patch.object(CornerstoneHandler, "_dismiss_cookie_banner"),
            patch.object(CornerstoneHandler, "_is_confirmation", staticmethod(lambda p: False)),
            patch.object(
                CornerstoneHandler, "_is_application_form", classmethod(lambda c, p: True)
            ),
            patch.object(CornerstoneHandler, "_step_key", staticmethod(lambda p: "Step 2 of 2")),
            patch.object(CornerstoneHandler, "_advance", staticmethod(fake_advance)),
        ):
            handler.on_step_start(page, ctx)
        assert seen["allow_submit"] is True

    def test_handler_ctx_carries_dry_run(self):
        """external.py must actually pass the flag through."""
        import inspect

        from jobapply.external import submit_external_apply

        src = inspect.getsource(submit_external_apply)
        assert '"dry_run": dry_run,' in src


class TestCornerstoneRadioSelection:
    """Screening answers were computed correctly then silently dropped.

    Regression (2026-08-26): step 2's form is long enough that every screening
    radio sits below the fold, so check() refused with "Element is outside of
    the viewport" and all seven answers (noncompete, prior employment, work
    authorization, visa, ITAR status, accuracy, data sharing) were lost.
    """

    def test_scrolls_into_view_before_checking(self):
        el = MagicMock()
        el.is_checked.return_value = True
        assert CornerstoneHandler._select_radio(el) is True
        el.scroll_into_view_if_needed.assert_called_once()

    def test_falls_back_to_label_click(self):
        el = MagicMock()
        el.check.side_effect = RuntimeError("Element is outside of the viewport")
        el.is_checked.return_value = True
        assert CornerstoneHandler._select_radio(el) is True
        el.evaluate.assert_called()

    def test_reports_failure_when_nothing_sticks(self):
        el = MagicMock()
        el.check.side_effect = RuntimeError("nope")
        el.evaluate.side_effect = RuntimeError("nope")
        el.is_checked.return_value = False
        assert CornerstoneHandler._select_radio(el) is False

    def test_unchecked_result_is_not_reported_as_success(self):
        """check() can pass while the widget stays unset."""
        el = MagicMock()
        el.is_checked.return_value = False
        assert CornerstoneHandler._select_radio(el) is False


class TestCornerstoneConsentQuestions:
    """Procedural acknowledgements must be Yes, not profile-guessed.

    Regression (2026-08-26): "Linde plc - recruitment privacy notice ..." was
    resolved by the generic Yes/No resolver, which answered No. Linde then
    refused every Submit with "Please acknowledge receipt of the respective
    recruitment privacy notice and confirm by clicking 'Yes'." -- all other
    fields valid.
    """

    def test_consent_patterns_cover_the_privacy_notice(self):
        from ats_handlers.csod import _CONSENT_PATTERNS

        q = (
            "Linde plc - recruitment privacy notice By submitting my personal and "
            "application data I acknowledge receipt of the notice"
        ).lower()
        assert any(p in q for p in _CONSENT_PATTERNS)

    def test_screening_questions_are_not_treated_as_consent(self):
        from ats_handlers.csod import _CONSENT_PATTERNS

        for q in (
            "do you currently work for a competitor of linde",
            "have you ever worked for linde or any of its affiliates",
            "will you now or in the future require a visa",
        ):
            assert not any(p in q for p in _CONSENT_PATTERNS), q


class TestCornerstoneConfirmationDetection:
    """Submitted applications were reported as failures.

    Regression (2026-08-26): both Linde applications went through -- the
    debug screenshots show "Thank You! You have successfully applied to
    <role>" -- but _is_confirmation only matched "successfully submitted", so
    the run fell through to the generic loop, stalled, and logged
    "failed: external form stuck (step 7/20)" for work that had succeeded.
    """

    def _page(self, text):
        page = MagicMock()
        page.evaluate.return_value = text.lower()
        return page

    def test_linde_wording_is_recognised(self):
        page = self._page("Thank You! You have successfully applied to Product Engineer II")
        assert CornerstoneHandler._is_confirmation(page) is True

    def test_other_confirmation_wordings_still_match(self):
        for text in (
            "Thank you for applying to Acme",
            "Your application has been submitted",
            "Application submitted",
            "Your application has been received",
        ):
            assert CornerstoneHandler._is_confirmation(self._page(text)) is True, text

    def test_form_page_is_not_a_confirmation(self):
        page = self._page("Step 2 of 2 Submit Application Cancel Save Back")
        assert CornerstoneHandler._is_confirmation(page) is False


class _SubmitBtn:
    def __init__(self, text="Submit", visible=True):
        self._t, self._v = text, visible

    def inner_text(self):
        return self._t

    def is_visible(self):
        return self._v


class TestWorkdaySubmitReview:
    """Submitting from the Review page must only report success on evidence."""

    def _page(self, states, buttons=None):
        page = MagicMock()
        page.query_selector_all.return_value = [_SubmitBtn()] if buttons is None else buttons
        page.evaluate.side_effect = list(states) + [states[-1]] * 20
        return page

    def _run(self, page):
        with patch("ats_handlers.workday._click_workday_button"):
            return WorkdayHandler()._submit_review(page)

    def test_confirmation_is_submitted(self):
        page = self._page(
            [
                {"text": "review ...", "errors": []},
                {"text": "congratulations! your application has been submitted.", "errors": []},
            ]
        )
        assert self._run(page) == "submitted"

    def test_validation_error_is_a_failure(self):
        page = self._page(
            [{"text": "review", "errors": ["Errors Found: Phone Number is required"]}]
        )
        result = self._run(page)
        assert result.startswith("failed") and "Phone Number is required" in result

    def test_no_confirmation_is_flagged_unconfirmed_not_success(self):
        page = self._page([{"text": "review page still here", "errors": []}])
        result = self._run(page)
        assert result != "submitted"
        assert result.startswith("submitted: unconfirmed")

    def test_missing_submit_button_fails_without_clicking(self):
        page = self._page([{"text": "review", "errors": []}], buttons=[_SubmitBtn("Next")])
        with patch("ats_handlers.workday._click_workday_button") as click:
            result = WorkdayHandler()._submit_review(page)
        click.assert_not_called()
        assert result.startswith("failed")
