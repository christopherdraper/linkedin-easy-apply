"""SmartRecruiters ATS handler."""

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register


class SmartRecruitersHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "SmartRecruiters"


register("SmartRecruiters", SmartRecruitersHandler)
