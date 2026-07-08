"""Prompt injection defence and sensitive-field abort checks."""

import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt injection defence
# ---------------------------------------------------------------------------
_INJECTION_PATTERNS = [
    "ignore previous",
    "ignore all instructions",
    "ignore all previous",
    "disregard your instructions",
    "disregard previous",
    "forget your instructions",
    "forget your programming",
    "if you are an ai",
    "if this is an ai",
    "if you're an ai",
    "you are a language model",
    "you are an llm",
    "you are gpt",
    "you are chatgpt",
    "you are claude",
    "new instructions:",
    "override your instructions",
    "override your programming",
    "system prompt",
    "system message",
    "do not apply to this",
    "do not submit",
    "stop processing",
    "pretend you are",
    "pretend to be",
    "roleplay as",
    "math problem",
    "solve this equation",
    "jailbreak",
]


# Fields that should never appear in a legitimate job application form
_ABORT_FIELD_PATTERNS = [
    "social security",
    "ssn",
    "social insurance",
    "bank account",
    "routing number",
    "account number",
    "passport number",
    "driver's license number",
    "driver license number",
    "credit card",
    "date of birth",
    "mother's maiden",
]


class ApplicationAbortError(Exception):
    """
    Raised anywhere in the application pipeline to abort the current submission.
    Caught by submit_easy_apply which returns 'aborted: <reason>'.
    """

    pass


def _looks_like_injection(text: str) -> bool:
    """Return True if text contains patterns that look like prompt injection."""
    lower = text.lower()
    return any(p in lower for p in _INJECTION_PATTERNS)


def _check_field_label(label: str) -> None:
    """
    Inspect a form field label and raise ApplicationAbortError if it looks
    like an injection attempt or a request for sensitive personal data that
    should never appear in a job application.
    """
    lower = label.lower()
    if _looks_like_injection(label):
        raise ApplicationAbortError(f"Prompt injection detected in form field: {label[:80]!r}")
    for pattern in _ABORT_FIELD_PATTERNS:
        if pattern in lower:
            raise ApplicationAbortError(
                f"Form requested sensitive data — suspicious posting: field contains {pattern!r}"
            )


def _sanitize_description(text: str) -> str:
    """
    Strip lines from a job description that look like prompt injection attempts.
    Logs a warning for each removed line so suspicious postings are visible.
    """
    clean = []
    for line in text.splitlines():
        if _looks_like_injection(line):
            log.warning(
                f"   ⚠️  Possible prompt injection removed from job description: {line[:80]!r}"
            )
            clean.append("[line removed]")
        else:
            clean.append(line)
    return "\n".join(clean)
