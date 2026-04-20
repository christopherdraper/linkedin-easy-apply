"""Workday ATS handler."""

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register


class WorkdayHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Workday"

    def pre_flight(self, page, ctx):
        self._dismiss_cookie_banner(page)
        return None

    def on_step_start(self, page, ctx):
        # "Start Your Application" popup -- "Autofill with Resume"
        try:
            autofill = page.query_selector("a[data-automation-id='autofillWithResume']")
            if autofill and autofill.is_visible():
                from job_search_apply import _safe_click

                _safe_click(autofill, page)
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:  # noqa: BLE001, S110
                    pass
                ctx["skip_step"] = True
                return None
        except Exception:  # noqa: BLE001, S110
            pass

        # Fallback: "Apply Manually"
        try:
            manual = page.query_selector("a[data-automation-id='applyManually']")
            if manual and manual.is_visible():
                from job_search_apply import _safe_click

                _safe_click(manual, page)
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:  # noqa: BLE001, S110
                    pass
                ctx["skip_step"] = True
                return None
        except Exception:  # noqa: BLE001, S110
            pass

        # Re-dismiss cookie banner (can reappear after navigation)
        self._dismiss_cookie_banner(page)
        return None

    def resolve_login_wall(self, page, ctx):
        return True

    def q2_pre_flight(self, page, ctx):
        self._dismiss_cookie_banner(page)
        return None

    def q2_resolve_login_wall(self, page, ctx):
        return True

    @staticmethod
    def _dismiss_cookie_banner(page):
        try:
            btn = page.query_selector(
                "#onetrust-accept-btn-handler, "
                "[data-testid='cookie-accept'], "
                "button:has-text('Accept All'):visible, "
                "button:has-text('Accept all'):visible, "
                "button:has-text('Accept Cookies'):visible"
            )
            if btn and btn.is_visible():
                from job_search_apply import _safe_click

                _safe_click(btn, page)
                page.wait_for_timeout(1000)
        except Exception:  # noqa: BLE001, S110
            pass


register("Workday", WorkdayHandler)
