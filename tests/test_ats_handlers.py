"""Tests for ATS handler base class and default handler."""

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
