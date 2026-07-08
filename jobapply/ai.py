"""Shared Anthropic client construction and availability flag."""

try:
    import anthropic as _anthropic

    _AI_AVAILABLE = True
except ImportError:
    _AI_AVAILABLE = False

_ai_client = None


def _get_ai_client():
    """Return a shared Anthropic client instance (created once, reused across calls)."""
    global _ai_client  # noqa: PLW0603
    if _ai_client is None:
        _ai_client = _anthropic.Anthropic()
    return _ai_client
