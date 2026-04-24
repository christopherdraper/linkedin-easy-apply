"""Lever ATS handler.

Lever has a 0% success rate in automated applications. This handler
lets attempts proceed normally but doesn't add any platform-specific
logic. If patterns emerge for why Lever fails, specific hooks can be
added here without touching generic code.

Prior attempt (reverted 2026-04-24): added a cookie-banner dismissal in
pre_flight after seeing a debug dump that contained only the cookie banner
HTML. Live inspection showed the Lever form renders with 13+ visible inputs
regardless of whether the banner is accepted -- the tiny dump was a
debug-capture artifact, not a true blocker. The real Lever blocker is
hCaptcha, which belongs in the captcha-solver path, not here.
"""

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register


class LeverHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Lever"


register("Lever", LeverHandler)
