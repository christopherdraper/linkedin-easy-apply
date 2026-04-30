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
        """Short-circuit embedded Ashby (``?ashby_jid=``) by navigating
        directly to the ``jobs.ashbyhq.com`` iframe URL, then check for a
        spam-banner rejection.

        Voleon, Vultr, and similar sites put the Ashby application form in
        an iframe on their careers page. The form-step loop never sees inputs
        on the outer page so it stalls at step 3. Mirroring the Greenhouse
        ``?gh_jid=`` iframe-jump fixes the same class of failure for Ashby.
        """
        if "ashby_jid=" in (page.url or "") and "ashbyhq.com" not in page.url:
            try:
                iframe = page.query_selector("iframe[src*='ashbyhq.com']")
                if iframe:
                    src = iframe.get_attribute("src") or ""
                    # Strip the embed=js suffix so we land on the full page,
                    # not the JS-only embed frame.
                    src = (
                        src.replace("&embed=js", "")
                        .replace("?embed=js&", "?")
                        .replace("?embed=js", "")
                    )
                    # Ashby's iframe lands on the job-detail page; rewrite to
                    # the /application suffix so the form-step loop sees inputs
                    # immediately rather than having to click 'Apply for this Job'.
                    if src and "/application" not in src:
                        # Insert /application before the query string
                        if "?" in src:
                            base, q = src.split("?", 1)
                            src = base.rstrip("/") + "/application?" + q
                        else:
                            src = src.rstrip("/") + "/application"
                    if src:
                        log.info("   Ashby: jumping to embedded application URL: %s", src[:100])
                        page.goto(src, timeout=30000)
                        page.wait_for_timeout(2000)
            except Exception as e:  # noqa: BLE001
                log.debug("Ashby embed iframe jump failed: %s", e)
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
