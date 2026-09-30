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

After the email step, iCIMS sends a known email to its shared sign-in,
login.icims.com (one account across every iCIMS employer): a username page,
then a password page. The applicant's password for it is stored under the
"login.icims.com" key of the ATS accounts file; without it the application is
skipped with a message saying so. The bot never resets that password: it is
the applicant's own account, not one the bot created.
"""

import logging

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register

log = logging.getLogger("job_apply")

_CONTENT_FRAME = "iframe#icims_content_iframe"
_SHARED_LOGIN_HOST = "login.icims.com"


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

    def resolve_login_wall(self, page, ctx: dict) -> bool:
        if _SHARED_LOGIN_HOST not in (page.url or ""):
            return False
        from jobapply.accounts import _load_ats_accounts
        from jobapply.forms import _safe_click

        acct = _load_ats_accounts().get(_SHARED_LOGIN_HOST) or {}
        password = acct.get("password")
        if not password:
            log.info(
                "   iCIMS: %s has an existing iCIMS account; store its password under "
                "'%s' in ats_accounts.json to apply on iCIMS sites",
                acct.get("email") or "the applicant",
                _SHARED_LOGIN_HOST,
            )
            return False
        try:
            user = page.query_selector("input[name='username']")
            if user and user.is_visible():
                user.fill(acct["email"])
                _safe_click(page.query_selector("button[type='submit'][name='action']"), page)
                page.wait_for_timeout(4000)
            pw = page.query_selector("input[name='password']")
            if not pw:
                return False
            pw.fill(password)
            _safe_click(page.query_selector("button[type='submit'][name='action']"), page)
            page.wait_for_timeout(6000)
        except Exception as e:  # noqa: BLE001
            log.debug("iCIMS shared sign-in failed: %s", e)
            return False
        signed_in = _SHARED_LOGIN_HOST not in page.url
        log.info("   iCIMS: shared sign-in %s", "succeeded" if signed_in else "was refused")
        return signed_in


register("iCIMS", ICIMSHandler)
