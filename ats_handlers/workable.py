"""Workable ATS handler.

Workable renders its cookie-consent prompt as ``<div role="dialog"
aria-modal="true" data-ui="cookie-consent">`` with a sibling
``data-ui="backdrop"`` that intercepts pointer events. Without dismissing
it first, the form-step loop fills fields successfully but the Submit
click is silently swallowed by the backdrop -- which manifests as
"form not progressing (no new fields filled)" because the page never
advances and there are no new fields to fill.

Verified live on apply.workable.com/mindex/j/5F72EE9A18/apply/: with the
cookie consent backdrop in place, clicking the apply-button times out
because the backdrop intercepts pointer events.
"""

from typing import Optional

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register


class WorkableHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Workable"

    def pre_flight(self, page, ctx: dict) -> Optional[str]:
        self._dismiss_cookie_banner(page)
        return None

    def on_step_start(self, page, ctx: dict) -> Optional[str]:
        # Cookie banner can re-appear after navigation; defensive re-dismiss
        # so the Submit click on the final step doesn't get backdrop-blocked.
        self._dismiss_cookie_banner(page)
        return None


register("Workable", WorkableHandler)
