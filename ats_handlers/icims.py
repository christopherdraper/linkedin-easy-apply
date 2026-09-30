"""iCIMS ATS handler.

iCIMS career portals render the job and the whole application inside
``iframe#icims_content_iframe``, wrapped in the employer's own site chrome.
The chrome has inputs and buttons of its own (CHA's portal: a job search box
and a banner "APPLY NOW" that goes to the generic job search), so the generic
iframe detection never switched into the frame, and the loop clicked the
banner button instead of "Apply for this job online" (2026-09-30).

Loading the frame URL (``in_iframe=1``) as the top page does not work: iCIMS
redirects it back to the wrapped portal. So the form loop works inside the
content frame instead.
"""

import logging

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register

log = logging.getLogger("job_apply")

_CONTENT_FRAME = "iframe#icims_content_iframe"


class ICIMSHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "iCIMS"

    def form_frame(self, page):
        try:
            frame_el = page.query_selector(_CONTENT_FRAME)
            frame = frame_el.content_frame() if frame_el else None
        except Exception as e:  # noqa: BLE001
            log.debug("iCIMS content frame lookup failed: %s", e)
            return None
        if frame:
            log.info("   iCIMS: working inside the application frame")
        return frame


register("iCIMS", ICIMSHandler)
