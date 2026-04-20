"""Base abstract class for ATS-specific lifecycle handlers.

Each ATS platform (Workday, Greenhouse, Lever, etc.) may subclass BaseATSHandler
and override only the hooks it needs.  All hooks have no-op defaults so subclasses
can be minimal.

Lifecycle overview
------------------
Q1 hooks (called from job_search_apply.py -> _navigate_external_form):

    pre_flight(page, ctx)
        Called once before the main form-step loop begins.
        Return None to continue normally; return a status string (e.g. "login_wall")
        to abort immediately with that status.

    on_step_start(page, ctx)
        Called at the top of each form-step loop iteration.
        Return None to continue; set ctx['skip_step'] = True to skip the rest of
        the current iteration (the loop continues to the next step without filling
        fields or clicking submit for this iteration).

    resolve_login_wall(page, ctx)
        Called when the generic login-wall detector fires.
        Return True if the handler successfully resolved it and the loop should
        continue; return False to fall through to the default login-wall logic.

    handle_verification_code(page, ctx)
        Called when an email-verification input is detected on the page.
        Return None to fall through to the generic verification code flow;
        return a status string to abort with that status.

    on_submit_clicked(page, ctx)
        Called immediately after the Submit button click is issued.
        Return None to continue with standard post-submit detection; return a
        status string to abort with that status.

    detect_success(page, ctx)
        Supplementary success-detection hook called alongside the generic detector.
        Return True if the handler considers the application submitted; return False
        to defer to the generic detector.

Q2 hooks (called from assisted_apply_mcp.py -> _run_page_loop):

    q2_pre_flight(page, ctx)
        Called once before the Q2 page loop begins.
        Same semantics as pre_flight: None to continue, status string to abort.

    q2_resolve_login_wall(page, ctx)
        Q2 counterpart of resolve_login_wall.
        Return True if resolved, False for default Q2 login-wall logic.

Handler state
-------------
Handlers are **stateless**.  All mutable per-application state is carried in the
``ctx`` dict that Q1/Q2 pass in.  Handlers must not store any instance-level state
that could bleed between application runs.
"""

from abc import ABC, abstractmethod
from typing import Optional


class BaseATSHandler(ABC):
    """Abstract base for per-platform ATS lifecycle handlers.

    Subclass this and override only the hooks your platform needs.
    All hooks have no-op implementations here; only ``platform_name`` is abstract.
    """

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Short identifier for the ATS platform, e.g. ``"workday"`` or ``"greenhouse"``."""

    # ------------------------------------------------------------------
    # Q1 hooks
    # ------------------------------------------------------------------

    def pre_flight(self, page, ctx: dict) -> Optional[str]:
        """Called before the Q1 form-step loop.

        Return None to continue, or a status string to abort.
        """
        return None

    def on_step_start(self, page, ctx: dict) -> Optional[str]:
        """Called at the top of each Q1 form-step iteration.

        Return None to continue.  Set ctx['skip_step'] = True to skip this
        iteration (the loop advances without filling fields or clicking submit).
        """
        return None

    def resolve_login_wall(self, page, ctx: dict) -> bool:
        """Called when the Q1 login-wall detector fires.

        Return True if the login wall was resolved; False to use default logic.
        """
        return False

    def handle_verification_code(self, page, ctx: dict) -> Optional[str]:
        """Called when an email-verification input is detected in Q1.

        Return None to fall through to generic verification handling, or a status
        string to abort with that status.
        """
        return None

    def on_submit_clicked(self, page, ctx: dict) -> Optional[str]:
        """Called immediately after the Q1 Submit button click.

        Return None to continue with standard post-submit detection, or a status
        string to abort with that status.
        """
        return None

    def detect_success(self, page, ctx: dict) -> bool:
        """Supplementary success detection for Q1.

        Return True if the application is considered submitted; False to defer to
        the generic detector.
        """
        return False

    # ------------------------------------------------------------------
    # Q2 hooks
    # ------------------------------------------------------------------

    def q2_pre_flight(self, page, ctx: dict) -> Optional[str]:
        """Called before the Q2 page loop.

        Return None to continue, or a status string to abort.
        """
        return None

    def q2_resolve_login_wall(self, page, ctx: dict) -> bool:
        """Called when the Q2 login-wall detector fires.

        Return True if the login wall was resolved; False to use default logic.
        """
        return False
