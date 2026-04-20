"""Default (no-op) ATS handler used when no platform-specific handler is registered."""

from ats_handlers._base import BaseATSHandler


class DefaultHandler(BaseATSHandler):
    """Fallback handler that provides no ATS-specific behaviour.

    All hooks inherit the no-op implementations from BaseATSHandler.
    This is used as a safe default when the active ATS does not match any
    registered platform handler.
    """

    @property
    def platform_name(self) -> str:
        return "unknown"
