"""Oracle Recruiting Cloud (ORC) ATS handler.

ORC candidate sites (Honeywell and others) draw their own checkboxes: the
real <input type="checkbox"> is transparent and 0x0 inside a <label>, and a
Knockout binding reads it. The generic filler only ticks visible checkboxes,
so the required privacy consent on the first step stayed unticked and every
Next failed with "You need to agree to the terms and conditions"
(2026-09-30). Clicking the label's text, clear of its policy link, ticks it.
Verified live on Honeywell's site.
"""

import logging
from typing import Optional

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register

log = logging.getLogger("job_apply")

_HIDDEN_REQUIRED_CHECKBOX = "label input[type='checkbox'].input-row__hidden-control[required]"


class OracleRecruitingHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Oracle Recruiting"

    def on_step_start(self, page, ctx: dict) -> Optional[str]:
        for box in page.query_selector_all(_HIDDEN_REQUIRED_CHECKBOX):
            try:
                if box.is_checked():
                    continue
                label = box.evaluate_handle("el => el.closest('label')").as_element()
                area = label.bounding_box() if label else None
                if not area:
                    continue
                # The label's left edge is its text, not the policy link.
                page.mouse.click(area["x"] + 5, area["y"] + area["height"] / 2)
                page.wait_for_timeout(500)
                if box.is_checked():
                    log.info("   Oracle: ticked required consent %r", label.inner_text()[:50])
            except Exception as e:  # noqa: BLE001
                log.debug("Oracle consent checkbox failed: %s", e)
        return None


register("Oracle Recruiting", OracleRecruitingHandler)
