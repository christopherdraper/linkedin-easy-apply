"""Playwright browser/session management and LinkedIn login helpers."""

import json
import logging
import os
import random
import time
from typing import Any, Dict, List, Optional

from jobapply.config import CDP_URL, CREDENTIALS_FILE, SESSION_FILE

log = logging.getLogger(__name__)

try:
    from playwright_stealth import Stealth

    _STEALTH = Stealth()
except ImportError:
    _STEALTH = None


# ---------------------------------------------------------------------------
# Human-like timing helpers
# ---------------------------------------------------------------------------
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
]


def _human_delay(base: float = 2.0, jitter: float = 3.0) -> None:
    """Sleep for a randomized duration to mimic human pacing."""
    delay = base + random.uniform(0, jitter)
    # Occasionally add a longer "thinking" pause (10% chance)
    if random.random() < 0.10:
        delay += random.uniform(5, 15)
    time.sleep(delay)


def _save_session(context) -> None:
    """Save Playwright session cookies with restricted file permissions (owner-only)."""
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(SESSION_FILE))
    os.chmod(SESSION_FILE, 0o600)


def _load_credentials() -> Optional[Dict[str, str]]:
    """Load LinkedIn credentials from the credentials file."""
    if not CREDENTIALS_FILE.exists():
        return None
    try:
        creds = json.loads(CREDENTIALS_FILE.read_text())
        if creds.get("email") and creds.get("password"):
            return creds
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _save_credentials(email: str, password: str) -> None:
    """Save LinkedIn credentials with restricted file permissions (owner-only)."""
    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_FILE.write_text(json.dumps({"email": email, "password": password}))
    os.chmod(CREDENTIALS_FILE, 0o600)


def _first_visible(page, selector: str):
    """Return the first visible element matching *selector*, or None."""
    try:
        for el in page.query_selector_all(selector):
            try:
                if el.is_visible():
                    return el
            except Exception:  # noqa: S112
                continue
    except Exception:  # noqa: S110
        pass
    return None


def _login_linkedin(page) -> bool:
    """
    Automate LinkedIn login using stored credentials.
    Handles email/password entry and verification challenges.
    Returns True if login succeeded, False otherwise.
    """
    creds = _load_credentials()
    if not creds:
        log.warning("⚠️  No credentials stored. Run with --setup to save LinkedIn credentials.")
        return False

    log.info("🔑 Logging into LinkedIn...")

    # Navigate to login page if not already there
    current = page.url
    if "login" not in current and "authwall" not in current:
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(2000)

    # Fill login form. LinkedIn's 2026 login page uses React-generated ids
    # and no name attributes; autocomplete/type are the stable hooks. The
    # page renders duplicate (desktop/mobile) copies, so pick the visible one.
    email_field = _first_visible(
        page,
        "input#username, input[name='session_key'], "
        "input[autocomplete='username'], input[type='email']",
    )
    pass_field = _first_visible(
        page,
        "input#password, input[name='session_password'], "
        "input[autocomplete='current-password'], input[type='password']",
    )
    if not email_field or not pass_field:
        log.error("❌ Could not find login form fields")
        return False

    email_field.fill(creds["email"])
    page.wait_for_timeout(500)
    pass_field.fill(creds["password"])
    page.wait_for_timeout(500)

    # :text-is (exact) not :has-text (substring): the SSO buttons
    # ("Sign in with Microsoft/Apple") would substring-match first.
    submit = _first_visible(
        page,
        "button[type='submit'], button[aria-label='Sign in'], button:text-is('Sign in')",
    )
    if submit:
        submit.click()
    else:
        pass_field.press("Enter")

    page.wait_for_timeout(4000)

    # Check if we landed on the feed (success)
    if "/feed" in page.url or "/jobs" in page.url:
        log.info("✅ Login successful")
        return True

    # Check for verification challenge
    if "checkpoint" in page.url or "challenge" in page.url:
        log.info("🔐 LinkedIn requires verification — check your email/phone for a code")
        for attempt in range(3):
            code = input("   Enter verification code (or 'skip' to abort): ").strip()
            if code.lower() == "skip":
                return False
            code_input = page.query_selector(
                "input#input__email_verification_pin, "
                "input[name='pin'], "
                "input#input__phone_verification_pin"
            )
            if code_input:
                code_input.fill(code)
                verify_btn = page.query_selector(
                    "button#email-pin-submit-button, "
                    "button[type='submit'], "
                    "button:has-text('Submit'), "
                    "button:has-text('Verify')"
                )
                if verify_btn:
                    verify_btn.click()
                else:
                    code_input.press("Enter")
                page.wait_for_timeout(3000)

                if "/feed" in page.url or "/jobs" in page.url:
                    log.info("✅ Verification successful")
                    return True
                log.warning(f"   ❌ Verification failed (attempt {attempt + 1}/3)")
            else:
                log.error("   ❌ Could not find verification code input field")
                return False
        return False

    # Check for wrong password
    error_el = page.query_selector(
        "[id*='error'], .alert-content, [role='alert'], p.form__label--error"
    )
    if error_el:
        error_text = error_el.inner_text().strip()
        log.error(f"❌ Login failed: {error_text}")
        return False

    log.warning(f"⚠️  Unexpected page after login: {page.url}")
    return False


def _stealth_playwright():
    """Return a stealth-wrapped sync_playwright() context manager if available."""
    from playwright.sync_api import sync_playwright

    if _STEALTH is not None:
        return _STEALTH.use_sync(sync_playwright())
    return sync_playwright()


def _resolve_proxy(
    url: str,
    proxy_rules: Dict[str, str],
    cli_proxy: Optional[str] = None,
    extra_urls: Optional[List[str]] = None,
) -> Optional[str]:
    """Pick a proxy for *url* based on per-ATS rules, falling back to CLI proxy.

    Also checks *extra_urls* (e.g. listing_url, description URLs) so that
    LinkedIn->SmartRecruiters redirects get the right proxy upfront.
    """
    if proxy_rules:
        from urllib.parse import urlparse

        all_urls = [url] + (extra_urls or [])
        for check_url in all_urls:
            host = urlparse(check_url).hostname or ""
            for pattern, proxy_url in proxy_rules.items():
                if pattern in host:
                    log.debug("Proxy rule matched: %s -> %s", pattern, proxy_url)
                    return proxy_url
    return cli_proxy


def _inject_session_cookies_if_needed(context) -> None:
    """Inject stored LinkedIn session cookies into a CDP context if auth is missing.

    When connecting to a shared Chromium via CDP, the context may lack the li_at
    authentication cookie, causing pages to render in guest/public mode. This
    loads cookies from the stored session file to restore authentication.
    """
    try:
        existing = context.cookies("https://www.linkedin.com")
        has_auth = any(c["name"] == "li_at" for c in existing)
        if has_auth:
            return
        if not SESSION_FILE.exists():
            log.debug("No session file to inject cookies from")
            return
        session_data = json.loads(SESSION_FILE.read_text())
        cookies = session_data.get("cookies", [])
        if not cookies:
            return
        # Filter to LinkedIn cookies only
        li_cookies = [c for c in cookies if ".linkedin.com" in c.get("domain", "")]
        if li_cookies:
            context.add_cookies(li_cookies)
            log.debug(
                "Injected %d LinkedIn cookies from session file into CDP context", len(li_cookies)
            )
    except Exception as exc:
        log.debug("Cookie injection failed (non-critical): %s", exc)


def _playwright_context(p, proxy: Optional[str] = None, headed: bool = False):
    """
    Connect to an existing Chromium via CDP if available, otherwise launch a new one.

    Returns (browser, context, page, owns_browser) where owns_browser is False
    when connected via CDP (caller must NOT close the browser).

    *headed* launches a visible browser (use with Xvfb on servers) which bypasses
    advanced anti-bot fingerprinting (e.g. DataDome).
    """
    # Try connecting to an already-running Chromium (preserves live LinkedIn session)
    # Skip CDP when a proxy is specified -- CDP browser doesn't route through it.
    if not headed and not proxy:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            # Inject stored session cookies if CDP context is missing LinkedIn auth
            _inject_session_cookies_if_needed(context)
            page = context.new_page()
            log.debug("Connected to existing Chromium via CDP (%s)", CDP_URL)
            return browser, context, page, False
        except Exception:  # noqa: S110
            log.debug("CDP connection unavailable, falling back to standalone browser")

    # Fallback: launch a standalone browser with stored cookies
    opts: Dict[str, Any] = {}
    if SESSION_FILE.exists():
        opts["storage_state"] = str(SESSION_FILE)
    if proxy:
        opts["proxy"] = {"server": proxy}

    launch_args = ["--disable-blink-features=AutomationControlled"]
    if not headed:
        launch_args.insert(0, "--headless=new")

    browser = p.chromium.launch(
        headless=not headed,
        args=launch_args,
    )
    # Randomize viewport slightly so sessions aren't identical
    vp_w = random.randint(1260, 1380)
    vp_h = random.randint(780, 900)
    context = browser.new_context(
        **opts,
        user_agent=random.choice(_USER_AGENTS),
        viewport={"width": vp_w, "height": vp_h},
        locale="en-US",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    # Stealth plugin handles webdriver override + fingerprint evasions automatically
    # Only add manual fallback if stealth is not available
    if _STEALTH is None:
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)
    page = context.new_page()
    mode = "headed (Xvfb)" if headed else "headless"
    log.debug("Launched standalone %s Chromium (cookie file)", mode)
    return browser, context, page, True


def _dismiss_linkedin_overlays(page) -> None:
    """Dismiss cookie consent banners and sign-in modals on LinkedIn search pages.

    When connecting via CDP to a shared browser, new tabs may render the public
    (guest) view with cookie consent and 'Sign in to view more jobs' overlays
    that block job card rendering.
    """
    page.wait_for_timeout(1500)

    # 1. Dismiss cookie consent banner
    try:
        cookie_btn = page.query_selector(
            "[data-tracking-control-name='ga-cookie.consent.accept.v4'], "
            "button.artdeco-global-alert-action:has-text('Accept')"
        )
        if cookie_btn and cookie_btn.is_visible():
            cookie_btn.click()
            log.debug("Dismissed LinkedIn cookie consent banner")
            page.wait_for_timeout(500)
    except Exception:  # noqa: S110
        pass

    # 2. Dismiss 'Sign in to view more jobs' modal
    try:
        modal_close = page.query_selector(
            "button.modal__dismiss[aria-label='Dismiss'], "
            "[data-tracking-control-name='public_jobs_contextual-sign-in-modal_modal_dismiss']"
        )
        if modal_close and modal_close.is_visible():
            modal_close.click()
            log.debug("Dismissed LinkedIn sign-in modal")
            page.wait_for_timeout(500)
    except Exception:  # noqa: S110
        pass

    # 3. Fallback: press Escape for any remaining modal
    try:
        modal = page.query_selector("[role='dialog']")
        if modal and modal.is_visible():
            page.keyboard.press("Escape")
            log.debug("Dismissed LinkedIn overlay via Escape key")
            page.wait_for_timeout(500)
    except Exception:  # noqa: S110
        pass


def _ensure_logged_in(page, target_url: str) -> None:
    """Check if LinkedIn redirected to auth wall and attempt auto-login. Raises on failure."""
    if "authwall" not in page.url and "uas/login" not in page.url:
        return
    if not _login_linkedin(page):
        raise RuntimeError(f"LinkedIn session expired. Redirected to: {page.url}")
    # Re-navigate to the intended page after login
    page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
    if "authwall" in page.url or "uas/login" in page.url:
        raise RuntimeError("Login succeeded but still redirected to auth wall")
