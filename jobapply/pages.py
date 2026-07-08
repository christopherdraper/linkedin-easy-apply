"""CAPTCHA detection/solving and external-ATS page handling: login walls,
cookie banners, page snapshots/classification, and confirmation detection."""

import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, Optional

from jobapply import stats
from jobapply.accounts import _attempt_account_creation, _attempt_ats_login, _get_domain
from jobapply.ai import _AI_AVAILABLE, _get_ai_client
from jobapply.forms import _safe_click
from jobapply.profile import ApplicantProfile

log = logging.getLogger(__name__)


def _detect_captcha(page) -> Optional[Dict[str, str]]:
    """Detect CAPTCHA on page. Returns dict with type/sitekey/url, or None."""
    try:
        info = page.evaluate("""() => {
            // hCaptcha — check BEFORE reCAPTCHA since both use [data-sitekey]
            const hcapFrame = document.querySelector('iframe[src*="hcaptcha"]');
            const hcapDiv = document.querySelector('.h-captcha, [data-hcaptcha-sitekey]');
            if (hcapFrame || hcapDiv) {
                let sitekey = '';
                if (hcapDiv) sitekey = hcapDiv.getAttribute('data-sitekey')
                    || hcapDiv.getAttribute('data-hcaptcha-sitekey') || '';
                if (!sitekey && hcapFrame) {
                    const m = hcapFrame.src.match(/sitekey=([^&]+)/);
                    if (m) sitekey = m[1];
                }
                return {type: 'hcaptcha', sitekey};
            }
            // reCAPTCHA v2/v3/Enterprise
            const recapFrame = document.querySelector('iframe[src*="recaptcha"]');
            const recapDiv = document.querySelector('.g-recaptcha, [data-sitekey]:not(.h-captcha)');
            const recapBadge = document.querySelector('.grecaptcha-badge');
            const isEnterprise = !!document.querySelector(
                'script[src*="recaptcha/enterprise"], iframe[src*="recaptcha/enterprise"]'
            );
            if (recapFrame || recapDiv || recapBadge) {
                let sitekey = '';
                // 1. data-sitekey attribute
                if (recapDiv) sitekey = recapDiv.getAttribute('data-sitekey') || '';
                // 2. k= param in iframe src
                if (!sitekey && recapFrame) {
                    const m = recapFrame.src.match(/[?&]k=([^&]+)/);
                    if (m) sitekey = m[1];
                }
                // 3. render= param in script src
                if (!sitekey) {
                    const scripts = document.querySelectorAll('script[src*="recaptcha"]');
                    for (const s of scripts) {
                        const m = s.src.match(/render=([^&]+)/);
                        if (m && m[1] !== 'explicit') { sitekey = m[1]; break; }
                    }
                }
                // 4. Extract from ___grecaptcha_cfg (runtime config object)
                if (!sitekey && window.___grecaptcha_cfg) {
                    try {
                        const clients = window.___grecaptcha_cfg.clients;
                        if (clients) {
                            for (const cid of Object.keys(clients)) {
                                const c = clients[cid];
                                // Walk nested objects looking for sitekey
                                const walk = (obj, depth) => {
                                    if (!obj || depth > 5) return '';
                                    for (const k of Object.keys(obj)) {
                                        if (k === 'sitekey' && typeof obj[k] === 'string')
                                            return obj[k];
                                        if (typeof obj[k] === 'object') {
                                            const r = walk(obj[k], depth + 1);
                                            if (r) return r;
                                        }
                                    }
                                    return '';
                                };
                                const found = walk(c, 0);
                                if (found) { sitekey = found; break; }
                            }
                        }
                    } catch(e) {}
                }
                const isV3 = !!recapBadge && !recapFrame;
                let type = isV3 ? 'recaptchav3' : 'recaptchav2';
                if (isEnterprise) type += '_enterprise';
                return {type, sitekey};
            }
            // Cloudflare Turnstile
            const cfFrame = document.querySelector('iframe[src*="challenges.cloudflare"]');
            const cfDiv = document.querySelector('.cf-turnstile, [data-turnstile-sitekey]');
            const cfScript = document.querySelector(
                'script[src*="challenges.cloudflare.com/turnstile"]'
            );
            // Also detect rendered Turnstile widget (dynamically created, no static class)
            const cfWidget = document.querySelector(
                '[id*="turnstile"], [class*="turnstile"]'
            );
            if (cfFrame || cfDiv || cfScript || cfWidget) {
                let sitekey = '';
                if (cfDiv) sitekey = cfDiv.getAttribute('data-sitekey')
                    || cfDiv.getAttribute('data-turnstile-sitekey') || '';
                // Try extracting sitekey from turnstile render calls
                if (!sitekey && window.turnstile && window.turnstile._configs) {
                    try {
                        for (const [k, v] of Object.entries(window.turnstile._configs)) {
                            if (v.sitekey) { sitekey = v.sitekey; break; }
                        }
                    } catch(e) {}
                }
                // Try extracting from script data attributes or nearby elements
                if (!sitekey) {
                    const allDivs = document.querySelectorAll('[data-sitekey]');
                    for (const d of allDivs) {
                        sitekey = d.getAttribute('data-sitekey') || '';
                        if (sitekey) break;
                    }
                }
                // Extract from inline config (e.g. Workable's turnstileWidgetSiteKey)
                if (!sitekey) {
                    const html = document.documentElement.innerHTML;
                    const m = html.match(/turnstile[a-zA-Z]*(?:Site)?Key["\\s:]+["']?(0x[0-9a-zA-Z_-]+)/i);
                    if (m) sitekey = m[1];
                }
                return {type: 'turnstile', sitekey};
            }
            // Generic text-based detection (no sitekey available)
            const body = document.body.innerText.toLowerCase();
            if (body.includes('flagged as possible spam')) return {type: 'unknown', sitekey: ''};
            if (body.includes('perform the security check'))
                return {type: 'unknown', sitekey: ''};
            if (body.includes('security checkpoint'))
                return {type: 'unknown', sitekey: ''};
            return null;
        }""")
        return info
    except Exception:
        return None


def _solve_captcha(
    page, captcha_info: Dict[str, str], api_key: str, service: str = "2captcha"
) -> bool:
    """Solve a CAPTCHA using a third-party solving service and inject the token."""
    ctype = captcha_info.get("type", "unknown")
    sitekey = captcha_info.get("sitekey", "")
    page_url = page.url

    if not sitekey:
        log.warning("   🧩 CAPTCHA detected (%s) but no sitekey found — cannot solve", ctype)
        return False

    if ctype == "unknown":
        log.warning("   🧩 Unknown CAPTCHA type — cannot solve")
        return False

    base = "https://2captcha.com" if service == "2captcha" else "https://api.capsolver.com"

    log.info("   🧩 Solving %s CAPTCHA via %s ...", ctype, service)

    # --- Submit task ---
    try:
        if service == "capsolver":
            solved = _capsolver_solve(api_key, ctype, sitekey, page_url)
        else:
            solved = _2captcha_solve(api_key, base, ctype, sitekey, page_url)
    except Exception as e:
        log.warning("   🧩 CAPTCHA solve failed: %s", e)
        return False

    if not solved:
        return False

    # --- Inject token ---
    return _inject_captcha_token(page, ctype, solved)


def _2captcha_solve(
    api_key: str, base: str, ctype: str, sitekey: str, page_url: str
) -> Optional[str]:
    """Submit and poll 2Captcha for a solution token."""
    import urllib.parse
    import urllib.request

    # Build request params
    params: Dict[str, str] = {
        "key": api_key,
        "method": "userrecaptcha",
        "googlekey": sitekey,
        "pageurl": page_url,
        "json": "1",
    }
    if "enterprise" in ctype:
        params["enterprise"] = "1"
    if ctype.startswith("recaptchav3"):
        params["version"] = "v3"
        params["action"] = "verify"
        params["min_score"] = "0.3"
    elif ctype == "hcaptcha":
        params["method"] = "hcaptcha"
        params["sitekey"] = sitekey
        del params["googlekey"]
    elif ctype == "turnstile":
        params["method"] = "turnstile"
        params["sitekey"] = sitekey
        del params["googlekey"]

    submit_url = f"{base}/in.php?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(submit_url)
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())

    if result.get("status") != 1:
        log.warning("   🧩 2Captcha submit error: %s", result.get("request", "unknown"))
        return None

    task_id = result["request"]
    log.info("   🧩 Task submitted (id=%s), polling for solution...", task_id)

    # Poll for result (max ~180s)
    for attempt in range(36):
        time.sleep(5)
        poll_url = f"{base}/res.php?key={api_key}&action=get&id={task_id}&json=1"
        try:
            with urllib.request.urlopen(poll_url, timeout=15) as resp:
                result = json.loads(resp.read())
        except Exception:
            continue

        if result.get("status") == 1:
            token = result["request"]
            log.info("   🧩 CAPTCHA solved (attempt %d)", attempt + 1)
            return token
        if result.get("request") != "CAPCHA_NOT_READY":
            log.warning("   🧩 2Captcha error: %s", result.get("request", "unknown"))
            return None

    log.warning("   🧩 2Captcha timed out after 180s")
    return None


def _capsolver_solve(api_key: str, ctype: str, sitekey: str, page_url: str) -> Optional[str]:
    """Submit and poll CapSolver for a solution token."""
    import urllib.request

    task_type_map = {
        "recaptchav2": "ReCaptchaV2TaskProxyLess",
        "recaptchav2_enterprise": "ReCaptchaV2EnterpriseTaskProxyLess",
        "recaptchav3": "ReCaptchaV3TaskProxyLess",
        "recaptchav3_enterprise": "ReCaptchaV3EnterpriseTaskProxyLess",
        "hcaptcha": "HCaptchaTaskProxyLess",
        "turnstile": "AntiTurnstileTaskProxyLess",
    }
    task_type = task_type_map.get(ctype)
    if not task_type:
        return None

    task: Dict = {
        "type": task_type,
        "websiteURL": page_url,
        "websiteKey": sitekey,
    }
    if ctype == "recaptchav3":
        task["pageAction"] = "verify"
        task["minScore"] = 0.3

    body = json.dumps({"clientKey": api_key, "task": task}).encode()
    req = urllib.request.Request(
        "https://api.capsolver.com/createTask",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    if result.get("errorId", 0) != 0:
        log.warning("   🧩 CapSolver error: %s", result.get("errorDescription", "unknown"))
        return None

    task_id = result.get("taskId")
    if not task_id:
        return None
    log.info("   🧩 Task submitted (id=%s), polling...", task_id)

    for attempt in range(36):
        time.sleep(5)
        poll_body = json.dumps({"clientKey": api_key, "taskId": task_id}).encode()
        poll_req = urllib.request.Request(
            "https://api.capsolver.com/getTaskResult",
            data=poll_body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(poll_req, timeout=15) as resp:
                result = json.loads(resp.read())
        except Exception:
            continue

        status = result.get("status")
        if status == "ready":
            solution = result.get("solution", {})
            token = solution.get("gRecaptchaResponse") or solution.get("token", "")
            if token:
                log.info("   🧩 CAPTCHA solved (attempt %d)", attempt + 1)
                return token
            return None
        if status != "processing":
            log.warning("   🧩 CapSolver error: %s", result.get("errorDescription", status))
            return None

    log.warning("   🧩 CapSolver timed out after 180s")
    return None


def _inject_captcha_token(page, ctype: str, token: str) -> bool:
    """Inject a solved CAPTCHA token into the page and trigger callbacks."""
    try:
        if ctype.startswith("recaptchav2") or ctype.startswith("recaptchav3"):
            page.evaluate(
                """(token) => {
                const ta = document.querySelector('#g-recaptcha-response, '
                    + '[name="g-recaptcha-response"]');
                if (ta) { ta.style.display = 'block'; ta.value = token; }
                document.querySelectorAll('textarea[id^="g-recaptcha-response"]')
                    .forEach(el => { el.value = token; });
                if (typeof ___grecaptcha_cfg !== 'undefined') {
                    const clients = ___grecaptcha_cfg.clients || {};
                    for (const key of Object.keys(clients)) {
                        const c = clients[key];
                        for (const k2 of Object.keys(c)) {
                            const v = c[k2];
                            if (v && typeof v === 'object') {
                                for (const k3 of Object.keys(v)) {
                                    const cb = v[k3];
                                    if (cb && typeof cb.callback === 'function') {
                                        cb.callback(token);
                                        return;
                                    }
                                }
                            }
                        }
                    }
                }
                if (window.captchaCallback) window.captchaCallback(token);
                if (window.onRecaptchaSuccess) window.onRecaptchaSuccess(token);
            }""",
                token,
            )
        elif ctype == "hcaptcha":
            page.evaluate(
                """(token) => {
                const ta = document.querySelector('[name="h-captcha-response"], '
                    + 'textarea[name="g-recaptcha-response"]');
                if (ta) { ta.style.display = 'block'; ta.value = token; }
                document.querySelectorAll('textarea[name*="captcha"]')
                    .forEach(el => { el.value = token; });
            }""",
                token,
            )
        elif ctype == "turnstile":
            page.evaluate(
                """(token) => {
                const inp = document.querySelector(
                    '[name="cf-turnstile-response"], input[name*="turnstile"]');
                if (inp) inp.value = token;
                if (window.turnstile) {
                    const widgets = document.querySelectorAll('.cf-turnstile');
                    widgets.forEach(w => {
                        const cb = w.getAttribute('data-callback');
                        if (cb && typeof window[cb] === 'function') window[cb](token);
                    });
                }
            }""",
                token,
            )
        else:
            return False

        log.info("   🧩 CAPTCHA token injected into page")
        page.wait_for_timeout(1000)
        return True
    except Exception as e:
        log.warning("   🧩 Failed to inject CAPTCHA token: %s", e)
        return False


_PAGE_CLASSIFIER_SYSTEM = (
    "You classify job application web pages to determine what automated actions are needed. "
    "Output ONLY valid JSON. Never add explanation or markdown."
)


_GUEST_SELECTORS = (
    "a:has-text('Continue as guest'), a:has-text('Apply without account'), "
    "a:has-text('Guest'), button:has-text('Continue as guest'), "
    "button:has-text('Apply without'), a:has-text('continue without'), "
    # Common alternatives
    "a:has-text('Apply as guest'), button:has-text('Apply as guest'), "
    "a:has-text('Apply manually'), button:has-text('Apply manually'), "
    "button:has-text('Skip sign in'), "
    "a:has-text('No thanks'), button:has-text('No thanks')"
)

# Avature/BMC-style pages show login + "First time here?" with resume upload options.
# These are not guest bypasses — they're resume upload triggers that need special handling.
_RESUME_UPLOAD_BYPASS_SELECTORS = (
    "button:has-text('From Device'), a:has-text('From Device'), "
    "button:has-text('Copy & Paste'), a:has-text('Copy & Paste')"
)


def _wait_and_dismiss_cookies(page) -> None:
    """Wait for JS rendering, dismiss cookie consent banners, and wait for form elements."""
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:  # noqa: S110
        pass
    try:
        cookie_btn = page.query_selector(
            "#onetrust-accept-btn-handler, "
            "#CybsideCookieBannerAcceptButton, "
            "[data-testid='cookie-accept'], "
            "button:has-text('Accept All'), "
            "button:has-text('Accept all'), "
            "button:has-text('Accept'), "
            "button:has-text('I agree'), "
            "button:has-text('Got it'), "
            "button:has-text('Agree'), "
            "a:has-text('Accept All')"
        )
        if cookie_btn and cookie_btn.is_visible():
            _safe_click(cookie_btn, page)
            page.wait_for_timeout(1500)
    except Exception:  # noqa: S110
        pass
    # Wait for JS-rendered form elements (SPA portals like Avature render via JS)
    try:
        page.wait_for_selector("input, textarea, select", timeout=10000)
    except Exception:  # noqa: S110
        # If no form elements appear after 10s, the page might be an SPA still loading.
        # Give it more time for JS framework hydration.
        page.wait_for_timeout(3000)


def _try_guest_bypass(page) -> bool:
    """Look for guest/bypass links on a login page and click if found."""
    try:
        guest_link = page.query_selector(_GUEST_SELECTORS)
        if guest_link and guest_link.is_visible():
            log.info("   🚪 Found guest/bypass option, clicking...")
            _safe_click(guest_link, page)
            page.wait_for_timeout(2000)
            return True
    except Exception:  # noqa: S110
        pass
    return False


def _try_resume_upload_bypass(page, profile: Optional["ApplicantProfile"]) -> bool:
    """Handle Avature/BMC-style pages where 'From Device' triggers resume upload.

    These pages show login + 'First time here?' section. Clicking 'From Device'
    opens a file picker or reveals a file input. We try the Playwright file chooser
    API first, then fall back to finding the underlying <input type='file'>.
    """
    if not profile or not profile.resume_path:
        return False
    resume_path = Path(profile.resume_path).expanduser()
    if not resume_path.exists():
        return False
    try:
        btn = page.query_selector(_RESUME_UPLOAD_BYPASS_SELECTORS)
        if not btn or not btn.is_visible():
            return False
        log.info("   📄 Found 'From Device' upload bypass, uploading resume...")
        # Try 1: Playwright file chooser API (handles native file dialogs)
        try:
            with page.expect_file_chooser(timeout=3000) as fc_info:
                _safe_click(btn, page)
            fc_info.value.set_files(str(resume_path))
            log.info(f"   📄 Uploaded resume via file chooser: {resume_path.name}")
            page.wait_for_timeout(3000)
            return True
        except Exception:
            pass
        # Try 2: Click revealed a file input — find and fill it
        _safe_click(btn, page)
        page.wait_for_timeout(2000)
        file_input = page.query_selector("input[type='file']")
        if file_input:
            file_input.set_input_files(str(resume_path))
            log.info(f"   📄 Uploaded resume via file input: {resume_path.name}")
            # Wait for the ATS to process the resume and navigate to the form
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:  # noqa: S110
                pass
            page.wait_for_timeout(3000)
            return True
        # Try 3: Clicking may have navigated to the form directly
        if "login" not in page.url.lower() and "signin" not in page.url.lower():
            log.info("   🚪 'From Device' navigated past login wall")
            return True
    except Exception as exc:
        log.debug("Resume upload bypass failed: %s", exc)
    return False


def _resolve_login_wall(page, profile: Optional["ApplicantProfile"]) -> bool:
    """Try to get past a login wall. Returns True if resolved (not blocked)."""
    if _try_guest_bypass(page):
        return True
    if _try_resume_upload_bypass(page, profile):
        return True
    if not profile:
        return False
    domain = _get_domain(page.url)
    if _attempt_ats_login(page, domain):
        return True
    if profile.auto_create_accounts:
        return _attempt_account_creation(page, profile)
    return False


def _detect_login_page(page) -> bool:
    """Return True if the current page appears to be a login/registration page.

    Detection only — does not attempt to resolve the login wall.
    """
    url = page.url.lower()
    if any(p in url for p in ("login", "signin", "sign-in", "register", "/auth", "/sso")):
        return True

    # JS-based: check for password/username inputs + login text phrases
    try:
        if page.evaluate("""() => {
            const inputs = [...document.querySelectorAll('input')];
            if (inputs.some(i => i.type === 'password')) return true;
            if (inputs.some(i =>
                /user|login/i.test((i.name||'')+(i.id||'')+(i.placeholder||''))
            )) return true;
            const t = (document.body?.innerText || '').toLowerCase().slice(0, 5000);
            const phrases = [
                'sign in to apply', 'log in to apply',
                'create an account to apply', 'create account to apply',
                'register to apply', 'sign in or create', 'log in or create',
                'first time here', 'forgot your password'
            ];
            return phrases.some(p => t.includes(p));
        }"""):
            return True
    except Exception:  # noqa: S110
        pass

    # HTML source scan (catches late-rendered forms)
    try:
        html = page.content().lower()
        if 'type="password"' in html or "type='password'" in html:
            return True
        if "first time here" in html and ("log in" in html or "username" in html):
            return True
    except Exception:  # noqa: S110
        pass

    # Check iframes
    try:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            if frame.query_selector("input[type='password']"):
                return True
    except Exception:  # noqa: S110
        pass

    return False


def _extract_page_snapshot(page, max_chars: int = 8000) -> str:
    """Extract a compact text representation of all form fields on the current page."""
    try:
        snapshot = page.evaluate("""() => {
            const lines = [];
            const seen = new Set();

            // Pierce Shadow DOM: recursively collect elements matching selector
            function deepQueryAll(selector, root) {
                const results = [...root.querySelectorAll(selector)];
                const allEls = root.querySelectorAll('*');
                for (const el of allEls) {
                    if (el.shadowRoot) {
                        results.push(...deepQueryAll(selector, el.shadowRoot));
                    }
                }
                return results;
            }

            function getLabel(el) {
                const root = el.getRootNode();
                // label[for=id]
                if (el.id) {
                    const lbl = root.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                    if (lbl) return lbl.innerText.trim();
                }
                // aria-label
                if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
                // aria-labelledby
                const lblBy = el.getAttribute('aria-labelledby');
                if (lblBy) {
                    const ref = (root.getElementById ? root : document).getElementById(lblBy);
                    if (ref) return ref.innerText.trim();
                }
                // walk up to find label in parent
                let parent = el.closest('fieldset, .form-group, [class*="form-field"], '
                    + '[class*="FormField"], [data-automation-id]');
                if (parent) {
                    const lbl = parent.querySelector('label, legend, [class*="label"]');
                    if (lbl && lbl !== el) return lbl.innerText.trim();
                }
                // preceding sibling label
                const prev = el.previousElementSibling;
                if (prev && (prev.tagName === 'LABEL' || prev.tagName === 'LEGEND'))
                    return prev.innerText.trim();
                // placeholder
                if (el.placeholder) return el.placeholder.trim();
                return '';
            }

            function isVisible(el) {
                try {
                    const style = window.getComputedStyle(el);
                    return style.display !== 'none' && style.visibility !== 'hidden'
                        && el.offsetWidth > 0;
                } catch(e) { return false; }
            }

            const els = deepQueryAll(
                'input, select, textarea, [role="combobox"], [role="listbox"], '
                + '[contenteditable="true"], button[type="submit"], '
                + 'button:not([type="button"]):not([aria-hidden="true"])',
                document
            );
            for (const el of els) {
                if (!isVisible(el)) continue;
                const tag = el.tagName.toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                if (type === 'hidden') continue;

                const key = el.id || el.name || el.getAttribute('data-automation-id') || '';
                if (key && seen.has(key)) continue;
                if (key) seen.add(key);

                const label = getLabel(el);
                const parts = ['[' + tag + (type ? ':' + type : '') + ']'];
                if (label) parts.push('label="' + label.slice(0, 80) + '"');
                if (el.placeholder) parts.push('placeholder="' + el.placeholder.slice(0, 50) + '"');
                if (el.id) parts.push('id="' + el.id + '"');
                if (el.name) parts.push('name="' + el.name + '"');
                if (el.required) parts.push('required');
                if (el.value && type !== 'password') parts.push('value="' + el.value.slice(0, 30) + '"');

                if (tag === 'select') {
                    const opts = Array.from(el.options)
                        .filter(o => o.value && o.text.trim())
                        .slice(0, 15)
                        .map(o => o.text.trim());
                    if (opts.length) parts.push('options="' + opts.join('|') + '"');
                }

                if (tag === 'button' || type === 'submit') {
                    parts.push('text="' + (el.innerText || el.value || '').trim().slice(0, 40) + '"');
                }

                const accept = el.getAttribute('accept');
                if (accept) parts.push('accept="' + accept + '"');

                lines.push(parts.join(' '));
                if (lines.length >= 60) break;
            }
            return lines.join('\\n');
        }""")
        return snapshot[:max_chars] if snapshot else ""
    except Exception as exc:
        log.debug("Failed to extract page snapshot: %s", exc)
        return ""


def _classify_page(snapshot: str, url: str) -> dict:
    """Classify a page as login/form/confirmation/error using AI."""
    default = {
        "page_type": "form",
        "has_required_login": False,
        "has_file_upload": False,
        "has_form_fields": True,
        "notes": "classifier fallback",
    }
    if not _AI_AVAILABLE or not snapshot:
        return default

    prompt = f"""Classify this job application page.

URL: {url}

Interactive elements found on page:
{snapshot}

Return ONLY this JSON structure:
{{
  "page_type": "form" | "login" | "file_upload" | "confirmation" | "error" | "job_search" | "unknown",
  "has_required_login": true | false,
  "has_file_upload": true | false,
  "has_form_fields": true | false,
  "notes": "one sentence"
}}

Definitions:
- login: the only fillable fields are email/password for account login
- file_upload: primary purpose is uploading a resume or cover letter document
- form: application fields are present (name, experience, work history, etc.)
- confirmation: application was accepted; page says thank you / received
- error: page shows an error, posting closed, or 404
- job_search: this is a job SEARCH or LISTING page (search filters, job cards, "Displaying X of Y"), NOT an application form. Company career portals with search/filter UI are job_search, not form.
A page may have has_file_upload=true AND has_form_fields=true."""

    try:
        client = _get_ai_client()
        response = client.messages.create(
            model="claude-sonnet-5",
            thinking={"type": "disabled"},
            max_tokens=200,
            system=_PAGE_CLASSIFIER_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        stats.add_ai_tokens(response.usage)
        raw = response.content[0].text.strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            result = json.loads(match.group())
            # Ensure all expected keys exist
            for key in default:
                if key not in result:
                    result[key] = default[key]
            return result
    except Exception as exc:
        log.debug("Page classifier failed: %s", exc)

    return default


def _detect_success_or_confirmation(page, snapshot: str) -> bool:
    """Heuristic check for application submission confirmation."""
    try:
        url = page.url.lower()
        if any(p in url for p in ("confirmation", "thank-you", "thankyou", "success", "/complete")):
            return True
    except Exception:  # noqa: S110
        pass

    check_text = snapshot.lower() if snapshot else ""
    if not check_text:
        try:
            check_text = page.evaluate(
                "document.body?.innerText?.toLowerCase()?.slice(0, 2000) || ''"
            )
        except Exception:
            return False

    confirmation_phrases = [
        "application submitted",
        "application received",
        "thank you for applying",
        "thanks for applying",
        "we've received your application",
        "successfully submitted",
        "application complete",
        "you have applied",
        "your application has been",
    ]
    return any(p in check_text for p in confirmation_phrases)
