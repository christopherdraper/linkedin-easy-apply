"""Stall watchdog for batch runs.

On 2026-09-29 a batch sat silent for an hour on a LinkedIn search after
LinkedIn restricted the account, and nothing in the log said where it was
stuck. Once start() is called, every pet() resets a faulthandler timer; if no
search or application makes progress for STALL_TIMEOUT_S, faulthandler dumps
every thread's stack to stderr (so the log shows where it hung) and exits the
process. Each application is logged as soon as it finishes, so exiting loses
at most the one in flight.
"""

import faulthandler
import sys

# The longest application on record took ~12 minutes (a Workday vision fill).
STALL_TIMEOUT_S = 25 * 60

_started = False


def start(timeout_s: int = STALL_TIMEOUT_S) -> None:
    """Enable the watchdog for this process and arm it."""
    global _started, _timeout_s
    _started = True
    _timeout_s = timeout_s
    pet()


def pet() -> None:
    """Reset the stall timer. A no-op until start() is called (tests, one-off runs)."""
    if _started:
        faulthandler.dump_traceback_later(_timeout_s, exit=True, file=sys.stderr)


def stop() -> None:
    """Disarm the watchdog."""
    global _started
    _started = False
    faulthandler.cancel_dump_traceback_later()


_timeout_s = STALL_TIMEOUT_S
