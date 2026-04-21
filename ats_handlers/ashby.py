"""Ashby ATS handler.

Ashby uses an application-level spam filter that blocks automated submissions.
This is NOT IP-based (proxy doesn't help). The filter may block:
- On submit (banner appears after clicking submit)
- On page load (banner appears immediately; we were flagged from prior attempts)

The handler checks both cases and returns a clean failure status, saving
CAPTCHA credits and avoiding wasted form-filling attempts.
"""

import logging
from typing import Optional

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register

log = logging.getLogger("job_apply")


class AshbyHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Ashby"

    def pre_flight(self, page, ctx: dict) -> Optional[str]:
        """Detect spam rejection shown on page load (before form filling)."""
        return self._check_spam_banner(page, "pre-flight")

    def on_step_start(self, page, ctx: dict) -> Optional[str]:
        """Detect spam rejection on each form-step iteration.

        Ashby is a React SPA; the pre_flight hook may run before the client
        hydrates and renders the spam banner. The form-step loop gives React
        multiple chances to finish rendering before we declare the form stuck.
        """
        return self._check_spam_banner(page, "per-step")

    def on_submit_clicked(self, page, ctx: dict) -> Optional[str]:
        """Detect spam rejection after clicking submit."""
        return self._check_spam_banner(page, "post-submit")

    @staticmethod
    def _check_spam_banner(page, when: str) -> Optional[str]:
        try:
            body_text = page.evaluate(
                "() => (document.body ? document.body.innerText : '').toLowerCase()"
            )
            if "flagged as possible spam" in body_text or "flagged as spam" in body_text:
                log.warning("   Ashby: application flagged as spam (%s)", when)
                return "failed: Ashby spam filter"
        except Exception:  # noqa: BLE001
            pass
        return None


register("Ashby", AshbyHandler)
