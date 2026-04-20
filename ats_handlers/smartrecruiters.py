"""SmartRecruiters ATS handler.

SmartRecruiters quirks handled here:
- Job listing -> /oneclick-ui/ application form navigation
- DataDome anti-bot slider CAPTCHA (intermittent)
- Iframe-based form detection (handled by generic code, not here)
"""

import logging
from typing import Optional

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register

log = logging.getLogger("job_apply")


class SmartRecruitersHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "SmartRecruiters"

    def pre_flight(self, page, ctx: dict) -> Optional[str]:
        """Navigate from job listing to /oneclick-ui/ form, handle DataDome."""
        url = page.url

        # Navigate from job listing page to /oneclick-ui/ application form
        if "smartrecruiters.com" in url and "/oneclick-ui/" not in url:
            sr_link = page.query_selector(
                "a.js-o-ats-btn[href*='oneclick-ui'], a[href*='oneclick-ui']"
            )
            if sr_link:
                sr_href = sr_link.get_attribute("href")
                if sr_href:
                    log.info("   SmartRecruiters: navigating to %s", sr_href[:80])
                    page.goto(sr_href, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(3000)

        # DataDome anti-bot check (intermittent slider CAPTCHA)
        body = page.content()[:4000].lower()
        if "captcha-delivery" in body or "verification required" in body:
            log.info("   SmartRecruiters: DataDome challenge detected, retrying...")
            page.wait_for_timeout(5000)
            page.reload(wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3000)
            body = page.content()[:4000].lower()
            if "captcha-delivery" in body or "verification required" in body:
                job = ctx.get("job", {})
                from job_search_apply import _dump_form_debug

                _dump_form_debug(page, job.get("id", ""), "DataDome anti-bot")
                return "failed: anti-bot challenge (DataDome)"

        return None


register("SmartRecruiters", SmartRecruitersHandler)
