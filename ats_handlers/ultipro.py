"""UKG / UltiPro (recruiting*.ultipro.com) handler.

UltiPro job boards are built from UKG web components: the apply control is a
custom element ``<ukg-button>Apply now</ukg-button>``, not an ``<a>`` or
``<button>``. Every tag-based selector in the generic apply-button lookup
therefore misses it, and the run stalls with "external form stuck" on a job
description page that has no form fields.

Playwright's role/text engines do pierce open shadow roots and match custom
elements, so the handler locates the control that way and clicks through to
the real application form.
"""

import logging

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register

log = logging.getLogger(__name__)

# Job-detail pages carry the apply control; the form itself lives behind it.
_APPLY_NAMES = ("Apply now", "Apply Now", "Apply")
_UKG_APPLY_SEL = (
    "ukg-button:has-text('Apply'), "
    "[role='button']:has-text('Apply'), "
    "a:has-text('Apply'), "
    "button:has-text('Apply')"
)


class UltiProHandler(BaseATSHandler):
    """Click through UKG's web-component apply control to reach the form."""

    @property
    def platform_name(self) -> str:
        return "UltiPro"

    def pre_flight(self, page, ctx):
        self._dismiss_cookie_banner(page)
        return None

    def on_step_start(self, page, ctx):
        # Only ever click apply once; afterwards we are on the form itself and
        # a stray "Apply" match would bounce us back out of it.
        if ctx.get("_ultipro_applied"):
            return None
        if self._has_form_fields(page):
            ctx["_ultipro_applied"] = True
            return None
        if self._click_apply(page):
            ctx["_ultipro_applied"] = True
            ctx["skip_step"] = True
        return None

    @staticmethod
    def _has_form_fields(page) -> bool:
        try:
            return bool(
                page.evaluate(
                    "() => document.querySelectorAll("
                    "'input[type=text], input[type=email], input[type=file], textarea, select'"
                    ").length > 2"
                )
            )
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _click_apply(page) -> bool:
        """Find and click the UKG apply control. Returns True if clicked."""
        # Role first: it pierces open shadow roots and matches custom elements.
        for name in _APPLY_NAMES:
            try:
                loc = page.get_by_role("button", name=name, exact=False)
                if loc.count():
                    loc.first.click(timeout=12000)
                    page.wait_for_timeout(3000)
                    log.info("   UltiPro: clicked '%s' (role)", name)
                    return True
            except Exception:  # noqa: BLE001, S110
                pass
        try:
            el = page.query_selector(_UKG_APPLY_SEL)
            if el:
                el.click(timeout=12000)
                page.wait_for_timeout(3000)
                log.info("   UltiPro: clicked apply control (selector)")
                return True
        except Exception:  # noqa: BLE001, S110
            pass
        return False


register("UltiPro", UltiProHandler)
