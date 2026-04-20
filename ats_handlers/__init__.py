"""ATS handler registry -- per-platform modules for ATS-specific quirks."""

# Import handler modules so they self-register
import ats_handlers.ashby  # noqa: F401
import ats_handlers.greenhouse  # noqa: F401
import ats_handlers.lever  # noqa: F401
import ats_handlers.smartrecruiters  # noqa: F401
import ats_handlers.workday  # noqa: F401
from ats_handlers._registry import get_handler, register  # noqa: F401
