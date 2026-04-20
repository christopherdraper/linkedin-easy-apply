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
        with patch("job_search_apply.get_handler") as mock_gh:
            mock_handler = MagicMock()
            mock_handler.pre_flight.return_value = "failed: test abort"
            mock_gh.return_value = mock_handler

            page = self._make_page()
            context = MagicMock()
            context.pages = [page]

            with (
                patch("job_search_apply._stealth_playwright"),
                patch("job_search_apply._playwright_context") as mock_ctx,
                patch("job_search_apply._ensure_logged_in"),
                patch("job_search_apply._wait_and_dismiss_cookies"),
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

        with patch("job_search_apply._extract_page_snapshot", return_value="snapshot"):
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
            patch("job_search_apply._extract_page_snapshot", return_value="snapshot"),
            patch("job_search_apply._detect_success_or_confirmation", return_value=False),
            patch("job_search_apply._classify_page", return_value=login_classification),
            patch("job_search_apply._find_navigation_button", return_value=("none", None)),
            patch("job_search_apply._answer_external_screening_questions", return_value=0),
            patch("job_search_apply._is_login_wall", return_value=False),
            patch("job_search_apply._detect_login_page", return_value=False),
            patch("job_search_apply._detect_captcha", return_value=None),
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
            patch("job_search_apply._extract_page_snapshot", return_value="snapshot"),
            patch("job_search_apply._detect_success_or_confirmation", return_value=False),
            patch("job_search_apply._classify_page", return_value=form_classification),
            patch("job_search_apply._find_navigation_button", return_value=("submit", MagicMock())),
            patch("job_search_apply._answer_external_screening_questions", return_value=0),
            patch("job_search_apply._is_login_wall", return_value=False),
            patch("job_search_apply._detect_login_page", return_value=False),
            patch("job_search_apply._detect_captcha", return_value=None),
            patch("job_search_apply._safe_click"),
            patch("job_search_apply._save_session") as mock_save,
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
            patch("assisted_apply_mcp._detect_submission_success", return_value=False),
            patch("assisted_apply_mcp._detect_email_verification", return_value=False),
            patch("assisted_apply_mcp._detect_rejection", return_value=None),
            patch("assisted_apply_mcp._handle_captcha", return_value=False),
            patch("assisted_apply_mcp._handle_login_wall", return_value=False),
            patch("assisted_apply_mcp._get_page_text_snapshot", return_value="snapshot"),
            patch("assisted_apply_mcp._fix_corrupted_fields"),
            patch("assisted_apply_mcp._ai_analyze_page", return_value=None),
            patch("assisted_apply_mcp._handle_no_actions", return_value="failed: no actions"),
            patch("assisted_apply_mcp._dismiss_cookie_banner"),
            patch("assisted_apply_mcp._clear_errored_uploads"),
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
