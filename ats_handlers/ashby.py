"""Ashby ATS handler."""

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register


class AshbyHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Ashby"


register("Ashby", AshbyHandler)
