"""Greenhouse ATS handler."""

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register


class GreenhouseHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Greenhouse"


register("Greenhouse", GreenhouseHandler)
