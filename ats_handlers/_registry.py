"""Handler registry -- maps ATS platform names to handler instances."""

from typing import Dict, Type

from ats_handlers._base import BaseATSHandler
from ats_handlers.default import DefaultHandler

# Platform name -> handler class
_HANDLER_MAP: Dict[str, Type[BaseATSHandler]] = {}

# Platform name -> cached singleton instance
_INSTANCES: Dict[str, BaseATSHandler] = {}

# Returned for unknown platforms
_DEFAULT = DefaultHandler()


def register(platform_name: str, handler_class: Type[BaseATSHandler]) -> None:
    """Register a handler class for a platform name.

    Clears any cached instance so the next call to get_handler() creates
    a fresh instance from the new class.
    """
    _HANDLER_MAP[platform_name] = handler_class
    _INSTANCES.pop(platform_name, None)


def get_handler(url: str) -> BaseATSHandler:
    """Return the handler for a URL.

    Uses _detect_ats_platform() to resolve URL -> platform name, then returns
    a cached singleton handler instance.  Returns _DEFAULT for unknown platforms.
    """
    # Lazy import to avoid circular imports: job_search_apply imports ats_handlers
    from job_search_apply import _detect_ats_platform  # noqa: PLC0415

    platform = _detect_ats_platform(url)
    if platform not in _HANDLER_MAP:
        return _DEFAULT

    if platform not in _INSTANCES:
        _INSTANCES[platform] = _HANDLER_MAP[platform]()
    return _INSTANCES[platform]
