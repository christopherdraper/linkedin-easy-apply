"""Page-state detection: submission success, rejection, and email verification."""

from typing import Optional


def _detect_submission_success(page) -> bool:
    """Check if the page shows signs of successful submission."""
    try:
        body_text = page.evaluate("() => document.body.innerText").lower()
        success_phrases = [
            "application submitted",
            "thank you for applying",
            "thanks for applying",
            "application received",
            "successfully submitted",
            "your application has been",
            "we have received your application",
            "application complete",
        ]
        return any(phrase in body_text for phrase in success_phrases)
    except Exception:
        return False


def _detect_rejection(page) -> Optional[str]:
    """Check if the page shows rejection/block messages that mean we should stop."""
    try:
        body_text = page.evaluate("() => document.body.innerText").lower()
        rejection_phrases = {
            "flagged as possible spam": "spam filter",
            "flagged as spam": "spam filter",
            "couldn't submit your application": "submission blocked",
            "could not submit": "submission blocked",
            "already applied": "duplicate application",
            "you have already applied": "duplicate application",
            "position has been filled": "position closed",
            "no longer accepting": "position closed",
        }
        for phrase, reason in rejection_phrases.items():
            if phrase in body_text:
                return reason
        return None
    except Exception:
        return None


def _detect_email_verification(page) -> bool:
    """Check if the page is showing an email verification code prompt."""
    try:
        return page.evaluate("""() => {
            const text = (document.body ? document.body.innerText : '').toLowerCase();
            return text.includes('verification code was sent')
                || (text.includes('security code') && text.includes('character code'))
                || (text.includes('enter the') && text.includes('code to confirm'))
                || (text.includes('enter the 6') && text.includes('code'))
                || text.includes('verify your email');
        }""")
    except Exception:
        return False
