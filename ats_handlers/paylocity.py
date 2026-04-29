"""Paylocity ATS handler.

Paylocity's apply flow opens with a 'forceUploadResumeModal' that has a
single visible button ('Upload Resume') and a hidden ``<input type="file">``.
Clicking the visible button only opens the OS file picker, so the generic
form loop spins for 20 steps without ever dismissing the modal or
populating fields.

Fix: in pre_flight, set the resume file directly on the hidden file input
via Playwright's ``set_input_files``. That dismisses the modal and lets
Paylocity's resume-parser auto-populate ~30 fields, which the generic
loop then completes.

Verified live on recruiting.paylocity.com/Recruiting/Jobs/Apply/3911622:
modal removed from DOM, 87 visible inputs rendered, 32 auto-filled.
"""

import logging
from typing import Optional

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register

log = logging.getLogger("job_apply")


class PaylocityHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Paylocity"

    def pre_flight(self, page, ctx: dict) -> Optional[str]:
        profile = ctx.get("profile")
        if not profile:
            return None
        resume_path = getattr(profile, "resume_path", None)
        if not resume_path:
            return None

        try:
            modal = page.query_selector("#forceUploadResumeModal")
            if not modal or not modal.is_visible():
                return None
            file_input = page.query_selector("#forceUploadResumeModal input[type='file']")
            if not file_input:
                return None
            log.info("   Paylocity: dismissing forceUploadResumeModal via direct upload")
            file_input.set_input_files(resume_path)
            page.wait_for_timeout(5000)
        except Exception as e:  # noqa: BLE001
            log.debug("Paylocity force-upload-modal handling failed: %s", e)
        return None


register("Paylocity", PaylocityHandler)
