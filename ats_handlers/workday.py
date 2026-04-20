"""Workday ATS handler."""

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register


class WorkdayHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Workday"


register("Workday", WorkdayHandler)
