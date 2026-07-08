"""Gmail IMAP verification-code fetching and ATS account management
(create/store/reuse accounts on external job sites)."""

import json
import logging
import os
import re
import time
from typing import Dict, Optional

from jobapply.config import ATS_ACCOUNTS_FILE
from jobapply.forms import _dump_form_debug, _safe_click
from jobapply.profile import ApplicantProfile

log = logging.getLogger(__name__)


def _extract_code_from_email_body(body: str) -> Optional[str]:
    """Extract a verification code from an email body string."""
    body_clean = re.sub(r"\s+", " ", body)
    # Primary: "code <optional words> : CODE" -- matches Greenhouse format
    m = re.search(r"code[^:]{0,40}:\s*([A-Za-z0-9]{6,10})\b", body_clean)
    if m:
        return m.group(1)
    # 6-10 char alphanumeric token near code/verify keywords (PageUp, generic OTP)
    for m in re.finditer(r"\b([A-Za-z0-9]{6,10})\b", body_clean):
        start = max(0, m.start() - 60)
        context = body_clean[start : m.end() + 60].lower()
        if any(kw in context for kw in ("code", "verify", "enter", "paste", "security", "login")):
            # Skip common false positives (names, words, domains)
            token = m.group(1)
            if token.isalpha() and token.lower() in body_clean.lower():
                # Pure-alpha tokens that appear as regular words are likely false positives
                # unless they look like codes (mixed case like aB3x)
                if token == token.lower() or token == token.capitalize():
                    continue
            return token
    return None


def _fetch_verification_code_from_gmail(  # noqa: C901
    email_addr: str, app_password: str, max_wait: int = 60
) -> Optional[str]:
    """Poll Gmail via IMAP for a recent verification code email and extract the code.

    Only considers UNSEEN emails from the last 5 minutes. Marks the email as
    read after extraction so it won't be picked up again on retry.
    Retries up to *max_wait* seconds waiting for a new email to arrive.
    """
    import email as email_mod
    import email.utils
    import imaplib
    from datetime import timezone

    search_queries = [
        '(FROM "greenhouse-mail" SUBJECT "security code" UNSEEN)',
        '(FROM "greenhouse" SUBJECT "security code" UNSEEN)',
        '(FROM "no-reply" SUBJECT "verification code" UNSEEN)',
        '(FROM "noreply" SUBJECT "security code" UNSEEN)',
        '(FROM "pageup" UNSEEN)',
        '(SUBJECT "one-time code" UNSEEN)',
        '(SUBJECT "verification" UNSEEN)',
    ]
    request_time = time.time()

    deadline = time.time() + max_wait
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        if attempt > 1:
            time.sleep(5)
        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com")
            imap.login(email_addr, app_password)
            imap.select("INBOX")

            for query in search_queries:
                _status, msg_ids = imap.search(None, query)
                if not msg_ids or not msg_ids[0]:
                    continue
                # Check messages newest-first
                for mid in reversed(msg_ids[0].split()):
                    _status, msg_data = imap.fetch(mid, "(RFC822)")
                    if not msg_data or not msg_data[0] or not isinstance(msg_data[0], tuple):
                        continue
                    raw = msg_data[0][1]
                    msg = email_mod.message_from_bytes(raw)

                    # Skip emails older than 5 minutes before we started
                    date_str = msg.get("Date", "")
                    if date_str:
                        parsed = email.utils.parsedate_to_datetime(date_str)
                        age = request_time - parsed.replace(tzinfo=timezone.utc).timestamp()
                        if age > 300:  # older than 5 min
                            continue

                    # Extract body (prefer plain text, fall back to stripped HTML)
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            ct = part.get_content_type()
                            if ct == "text/plain":
                                payload = part.get_payload(decode=True)
                                if payload:
                                    body = payload.decode("utf-8", errors="replace")
                                    break
                            elif ct == "text/html" and not body:
                                payload = part.get_payload(decode=True)
                                if payload:
                                    body = re.sub(
                                        r"<[^>]+>", " ", payload.decode("utf-8", errors="replace")
                                    )
                    else:
                        payload = msg.get_payload(decode=True)
                        if payload:
                            body = payload.decode("utf-8", errors="replace")

                    if not body:
                        continue

                    code = _extract_code_from_email_body(body)
                    if code:
                        # Mark as read so we don't re-use this code
                        imap.store(mid, "+FLAGS", "\\Seen")
                        log.info("   📧 Verification code from email: %s", code)
                        imap.logout()
                        return code

            imap.logout()
        except Exception as exc:
            log.debug("Gmail IMAP attempt %d failed: %s", attempt, exc)

    log.warning("   ⚠️ Could not retrieve verification code from email after %ds", max_wait)
    return None


# ---------------------------------------------------------------------------
# ATS account management — create/store/reuse accounts on external job sites
# ---------------------------------------------------------------------------


def _load_ats_accounts() -> Dict[str, Dict[str, str]]:
    """Load stored ATS accounts keyed by domain."""
    if ATS_ACCOUNTS_FILE.exists():
        try:
            return json.loads(ATS_ACCOUNTS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_ats_account(domain: str, email: str, password: str) -> None:
    """Store ATS credentials for a domain."""
    accounts = _load_ats_accounts()
    accounts[domain] = {"email": email, "password": password}
    ATS_ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ATS_ACCOUNTS_FILE.write_text(json.dumps(accounts, indent=2))
    os.chmod(ATS_ACCOUNTS_FILE, 0o600)
    log.info(f"   🔑 Stored ATS account for {domain}")


def _generate_ats_password() -> str:
    """Generate a strong password that satisfies most ATS requirements."""
    import secrets
    import string

    # 16 chars: mix of upper, lower, digits, special — meets virtually all policies
    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(16))
        # Ensure at least one of each required category
        if (
            any(c.isupper() for c in pw)
            and any(c.islower() for c in pw)
            and any(c.isdigit() for c in pw)
            and any(c in "!@#$%&*" for c in pw)
        ):
            return pw


def _get_domain(url: str) -> str:
    """Extract domain from URL for account keying."""
    from urllib.parse import urlparse

    return urlparse(url).netloc.lower()


def _attempt_ats_login(page, domain: str) -> bool:
    """Try to log in using stored ATS credentials. Returns True if login succeeded."""
    accounts = _load_ats_accounts()
    if domain not in accounts:
        return False

    acct = accounts[domain]
    log.info(f"   🔑 Found stored account for {domain}, attempting login")

    try:
        # If on a Create Account page, navigate to Sign In first
        # (e.g. Workday shows "Already have an account? Sign In")
        body_text = page.evaluate("document.body?.innerText?.slice(0, 3000) || ''").lower()
        if "create account" in body_text and "already have an account" in body_text:
            # Find the "Sign In" link adjacent to "Already have an account?" text.
            # Must NOT match the header Sign In button (which opens a modal instead).
            signin_link = page.evaluate_handle("""() => {
                const texts = document.querySelectorAll('*');
                for (const el of texts) {
                    if (el.textContent?.includes('Already have an account')
                        && el.children?.length <= 3) {
                        const btn = el.querySelector('button, a');
                        if (btn && /sign.in/i.test(btn.textContent)) return btn;
                    }
                }
                // Fallback: Sign In button inside main content (not header/banner)
                return document.querySelector('main button:has-text("Sign In")');
            }""").as_element()
            if signin_link and signin_link.is_visible():
                log.info("   🔗 Clicking 'Sign In' to switch from Create Account")
                _safe_click(signin_link, page)
                page.wait_for_timeout(3000)

        # Find and fill email/username field
        email_field = page.query_selector(
            "input[type='email'], input[name*='email'], input[name*='user'], "
            "input[id*='email'], input[id*='user'], input[autocomplete='email'], "
            "input[autocomplete='username'], "
            "input[data-automation-id='email']"  # Workday
        )
        if not email_field:
            log.debug("No email/username field found for ATS login (title=%s)", page.title())
            return False
        email_field.fill(acct["email"])

        # Find and fill password field
        pass_field = page.query_selector("input[type='password']")
        if not pass_field:
            log.debug("No password field found for ATS login")
            return False
        pass_field.fill(acct["password"])

        # Find and click login/sign-in button.
        # Workday uses click_filter overlay divs that intercept pointer events;
        # check for these FIRST (separate query) since query_selector returns
        # by DOM order and the overlay sits after the button it covers.
        login_btn = page.query_selector(
            "div[data-automation-id='click_filter'][aria-label='Sign In'], "
            "div[data-automation-id='click_filter'][aria-label='Log In']"
        )
        if not login_btn:
            login_btn = page.query_selector(
                "main button:has-text('Sign in'), form button:has-text('Sign in'), "
                "main button:has-text('Log in'), form button:has-text('Log in'), "
                "main button:has-text('Login'), form button:has-text('Login'), "
                "button[data-automation-id='signInSubmitButton'], "
                "button:has-text('Sign in'), button:has-text('Log in'), "
                "button:has-text('Login'), "
                "button[type='submit'], input[type='submit']"
            )
        if login_btn:
            _safe_click(login_btn, page)
            page.wait_for_timeout(5000)

            # Check if login succeeded (no longer on login page)
            still_has_password = page.query_selector("input[type='password']:visible")
            if not still_has_password and not any(
                p in page.url.lower() for p in ("login", "signin", "sign-in", "/auth")
            ):
                log.info("   ✅ ATS login succeeded")
                return True

            # Check for error messages
            errors = page.evaluate("document.body?.innerText?.toLowerCase()?.slice(0, 2000) || ''")
            if any(e in errors for e in ("invalid", "incorrect", "wrong password", "failed")):
                log.warning("   ⚠️ ATS login failed — credentials may be outdated")
                return False

        return False
    except Exception as exc:
        log.debug("ATS login attempt failed: %s", exc)
        return False


def _fill_registration_form(page, profile: "ApplicantProfile", password: str) -> int:
    """Fill registration form fields. Returns count of fields filled."""
    filled = 0
    try:
        # Name fields (try split first, then combined)
        first_f = page.query_selector(
            "input[name*='first' i], input[id*='first' i], "
            "input[autocomplete='given-name'], input[placeholder*='First' i]"
        )
        last_f = page.query_selector(
            "input[name*='last' i], input[id*='last' i], "
            "input[autocomplete='family-name'], input[placeholder*='Last' i]"
        )
        if first_f and last_f:
            parts = profile.full_name.split(None, 1)
            first_f.fill(parts[0])
            last_f.fill(parts[1] if len(parts) > 1 else "")
            filled += 2
        else:
            name_f = page.query_selector(
                "input[name*='name' i]:not([name*='user']):not([type='password']), "
                "input[autocomplete='name']"
            )
            if name_f:
                name_f.fill(profile.full_name)
                filled += 1

        # Email
        email_f = page.query_selector(
            "input[type='email'], input[name*='email' i], input[id*='email' i], "
            "input[autocomplete='email'], input[placeholder*='email' i]"
        )
        if email_f:
            email_f.fill(profile.email)
            filled += 1

        # Password fields (password + confirm)
        for pf in page.query_selector_all("input[type='password']"):
            if pf.is_visible():
                pf.fill(password)
                filled += 1

        # Phone
        phone_f = page.query_selector(
            "input[type='tel'], input[name*='phone' i], input[id*='phone' i]"
        )
        if phone_f and phone_f.is_visible():
            phone_f.fill(profile.phone)
            filled += 1

        # Terms checkbox
        terms_cb = page.query_selector(
            "input[type='checkbox'][name*='terms' i], "
            "input[type='checkbox'][name*='agree' i], "
            "input[type='checkbox'][id*='terms' i], "
            "input[type='checkbox'][id*='agree' i], "
            "input[type='checkbox'][name*='policy' i]"
        )
        if terms_cb and not terms_cb.is_checked():
            terms_cb.check()
    except Exception as exc:
        log.debug("Error filling registration fields: %s", exc)
    return filled


def _handle_registration_verification(page, profile: "ApplicantProfile") -> bool:
    """Handle email verification after registration. Returns False if blocked."""
    try:
        body_text = page.evaluate("document.body?.innerText?.toLowerCase()?.slice(0, 3000) || ''")
        verification_phrases = (
            "verification email",
            "verify your email",
            "check your email",
            "confirmation email",
            "sent you an email",
            "verify your account",
        )
        if not any(p in body_text for p in verification_phrases):
            return True  # No verification needed

        if not profile.gmail_app_password:
            log.warning("   ⚠️ Email verification required but no gmail_app_password")
            return False

        log.info("   📧 Email verification required, checking Gmail...")
        code = _fetch_verification_code_from_gmail(
            profile.email, profile.gmail_app_password, max_wait=45
        )
        if code:
            code_field = page.query_selector(
                "input[name*='code' i], input[name*='verif' i], "
                "input[placeholder*='code' i], input[type='text']:visible"
            )
            if code_field:
                code_field.fill(code)
                verify_btn = page.query_selector(
                    "button:has-text('Verify'), button:has-text('Confirm'), button[type='submit']"
                )
                if verify_btn:
                    _safe_click(verify_btn, page)
                    page.wait_for_timeout(3000)
            return True

        # No code found — check if it's a link-click verification
        page.wait_for_timeout(5000)
        body_text = page.evaluate("document.body?.innerText?.toLowerCase()?.slice(0, 3000) || ''")
        if any(w in body_text for w in ("click the link", "follow the link")):
            log.warning("   ⚠️ Email verification requires link click — cannot automate")
            return False
        return True
    except Exception as exc:
        log.debug("Verification handling failed: %s", exc)
        return True


def _attempt_account_creation(page, profile: "ApplicantProfile") -> bool:
    """Try to create an account on the current ATS page.

    Looks for registration form or 'Create Account' link, fills it using
    profile data, handles email verification, and stores credentials.
    Returns True if account creation succeeded and we're past the login wall.
    """
    domain = _get_domain(page.url)

    # Step 1: Navigate to registration form
    try:
        create_link = page.query_selector(
            "a:has-text('Create Account'), a:has-text('Create an Account'), "
            "a:has-text('Register'), a:has-text('Sign Up'), a:has-text('Sign up'), "
            "button:has-text('Create Account'), button:has-text('Register'), "
            "button:has-text('Sign Up'), button:has-text('Sign up'), "
            "a:has-text('New User'), a:has-text('Create a new account'), "
            "a:has-text('Don\\'t have an account')"
        )
        if create_link:
            log.info("   📝 Found account creation option, clicking...")
            _safe_click(create_link, page)
            page.wait_for_timeout(2000)
    except Exception as exc:
        log.debug("Could not find create account link: %s", exc)

    # Step 2: Fill and submit registration form
    password = _generate_ats_password()
    fields_filled = _fill_registration_form(page, profile, password)

    # Find submit/continue button
    submit_btn = None
    try:
        submit_btn = page.query_selector(
            "button[type='submit'], button:has-text('Continue'), "
            "button:has-text('Create'), button:has-text('Register'), "
            "button:has-text('Sign Up'), button:has-text('Submit'), "
            "input[type='submit']"
        )
    except Exception:  # noqa: S110
        pass

    # Email-first flows (ADP, etc.): only email field + Continue button
    if fields_filled == 1 and submit_btn:
        log.info("   📝 Email-first auth flow — clicking Continue...")
        _safe_click(submit_btn, page)
        page.wait_for_timeout(3000)
        # After Continue, we may get a password creation page
        pw_fields = page.query_selector_all("input[type='password']")
        if pw_fields:
            for pf in pw_fields:
                if pf.is_visible():
                    pf.fill(password)
            # Look for name fields that may have appeared
            _fill_registration_form(page, profile, password)
            submit_btn = page.query_selector(
                "button[type='submit'], button:has-text('Continue'), "
                "button:has-text('Create'), button:has-text('Submit')"
            )
            if submit_btn:
                _safe_click(submit_btn, page)
                page.wait_for_timeout(3000)
    elif fields_filled < 2:
        log.info("   ⚠️ Could not fill enough registration fields (%d)", fields_filled)
        _dump_form_debug(page, domain, "Account creation: too few fields filled")
        return False
    else:
        log.info(f"   📝 Filled {fields_filled} registration fields, submitting...")
        if not submit_btn:
            log.info("   ⚠️ No submit button found for registration")
            return False
        _safe_click(submit_btn, page)
        page.wait_for_timeout(3000)

    # Step 3: Handle email verification
    if not _handle_registration_verification(page, profile):
        return False

    # Step 4: Check outcome — use both URL and page content to detect failure
    page.wait_for_timeout(2000)
    body = page.evaluate("document.body?.innerText?.toLowerCase()?.slice(0, 2000) || ''")
    still_login = any(
        p in page.url.lower() for p in ("login", "signin", "sign-in", "register", "/auth")
    )
    # Also check page content for signs we're still on a login/registration page
    has_password_field = bool(page.query_selector("input[type='password']:visible"))
    if still_login or has_password_field:
        if any(
            s in body
            for s in (
                "already exists",
                "already registered",
                "account with that email",
                "email is already",
            )
        ):
            log.info("   ℹ️ Account already exists for %s, trying login", domain)
            return _attempt_ats_login(page, domain)
        if not any(s in body for s in ("account created", "registration successful", "welcome")):
            log.info("   ⚠️ Registration may not have succeeded (password field still visible)")
            return False
        page.wait_for_timeout(3000)

    _save_ats_account(domain, profile.email, password)
    log.info(f"   ✅ Account created on {domain}")
    return True
