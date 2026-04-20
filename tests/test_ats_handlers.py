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
