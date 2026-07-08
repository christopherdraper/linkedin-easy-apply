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


from ats_handlers.workday import WorkdayHandler  # noqa: E402


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
    def test_platform_name(self):
        assert LeverHandler().platform_name == "Lever"

    def test_all_hooks_are_noop(self):
        """Lever is a pure passthrough -- all hooks return defaults."""
        handler = LeverHandler()
        page = MagicMock()
        ctx = {}
        assert handler.pre_flight(page, ctx) is None
        assert handler.on_step_start(page, ctx) is None
        assert handler.resolve_login_wall(page, ctx) is False
        assert handler.handle_verification_code(page, ctx) is None
        assert handler.on_submit_clicked(page, ctx) is None
        assert handler.detect_success(page, ctx) is False


class TestAshbyHandler:
    def test_platform_name(self):
        assert AshbyHandler().platform_name == "Ashby"

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
