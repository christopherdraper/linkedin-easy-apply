"""Lever ATS handler.

Lever has a 0% success rate in automated applications. This handler
lets attempts proceed normally but doesn't add any platform-specific
logic. If patterns emerge for why Lever fails, specific hooks can be
added here without touching generic code.
"""

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register


class LeverHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Lever"


register("Lever", LeverHandler)
