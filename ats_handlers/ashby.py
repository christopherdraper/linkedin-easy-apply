"""Ashby ATS handler.

Ashby uses an application-level spam filter that blocks automated submissions.
This is NOT IP-based (proxy doesn't help). The handler detects the spam
rejection after form submission and returns a clean failure status.
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

    def on_submit_clicked(self, page, ctx: dict) -> Optional[str]:
        """Detect Ashby spam filter rejection after submit."""
        try:
            body_text = page.evaluate(
                "() => (document.body ? document.body.innerText : '').toLowerCase()"
            )
            if "flagged as possible spam" in body_text or "flagged as spam" in body_text:
                log.warning("   Ashby: application flagged as spam")
                return "failed: Ashby spam filter"
        except Exception:
            pass
        return None


register("Ashby", AshbyHandler)
