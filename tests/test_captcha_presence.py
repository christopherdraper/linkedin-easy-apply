"""Captcha detection and spam-trap fields, checked against a real DOM.

The detection script decides from rendered layout (size, display, visibility,
footer and form context), which a mocked page cannot exercise, so these tests
drive headless Chromium and skip where no browser is installed.

On 2026-09-29 Honeywell (an invisible hCaptcha) and CHA (a footer newsletter
reCAPTCHA) both failed as "captcha solve failed", and on 2026-09-30 the filler
answered Honeywell's honeypot field.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobapply.forms import _get_field_label, _is_trap_field  # noqa: E402
from jobapply.pages import _detect_captcha  # noqa: E402


@pytest.fixture(scope="module")
def browser_page():
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        pw = sync_api.sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as exc:  # no browser binaries (CI)
        pytest.skip(f"headless Chromium unavailable: {exc}")
    page = browser.new_page()
    yield page
    browser.close()
    pw.stop()


APPLICATION_FORM = '<form><label for="e">Email</label><input id="e" type="email"></form>'


class TestCaptchaPresence:
    def test_invisible_hcaptcha_is_not_a_blocker(self, browser_page):
        browser_page.set_content(
            APPLICATION_FORM
            + '<div aria-hidden="true"><iframe src="data:text/html,hcaptcha#frame=checkbox-invisible"'
            ' style="display:none"></iframe></div>'
            '<iframe src="data:text/html,hcaptcha#frame=challenge" style="visibility:hidden"></iframe>'
        )
        assert _detect_captcha(browser_page) is None

    def test_turnstile_script_without_widget_is_not_a_blocker(self, browser_page):
        # Dover (2026-10-03): api.js?render=explicit loaded, widget drawn only at submit.
        browser_page.set_content(
            APPLICATION_FORM
            + '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit">'
            "</script>"
        )
        assert _detect_captcha(browser_page) is None

    def test_visible_turnstile_widget_is_detected(self, browser_page):
        browser_page.set_content(
            APPLICATION_FORM
            + '<div class="cf-turnstile" data-sitekey="0xAAAA" style="width:300px;height:65px"></div>'
        )
        assert _detect_captcha(browser_page) == {"type": "turnstile", "sitekey": "0xAAAA"}

    def test_shadow_rendered_turnstile_found_by_its_response_input(self, browser_page):
        # Dover: the iframe sits in a closed shadow root; only the input is visible to JS.
        widget = (
            '<div style="width:300px;height:65px"><div></div>'
            '<input type="hidden" name="cf-turnstile-response" value="{}"></div>'
        )
        browser_page.set_content(APPLICATION_FORM + widget.format(""))
        assert (_detect_captcha(browser_page) or {}).get("type") == "turnstile"
        # Already solved: not reported again, so it is not paid for twice.
        browser_page.set_content(APPLICATION_FORM + widget.format("tok"))
        assert _detect_captcha(browser_page) is None

    def test_footer_newsletter_recaptcha_is_not_a_blocker(self, browser_page):
        browser_page.set_content(
            APPLICATION_FORM + "<footer><form><p>Join our mailing list</p><input type=email>"
            '<div class="g-recaptcha-container" data-sitekey="footer-key"'
            ' style="width:304px;height:78px"></div></form></footer>'
        )
        assert _detect_captcha(browser_page) is None

    def test_newsletter_form_outside_a_footer_is_not_a_blocker(self, browser_page):
        browser_page.set_content(
            APPLICATION_FORM + "<form><p>Subscribe to our newsletter</p><input type=email>"
            '<div class="g-recaptcha" data-sitekey="nl-key" style="width:304px;height:78px"></div></form>'
        )
        assert _detect_captcha(browser_page) is None

    def test_visible_recaptcha_in_the_application_is_detected(self, browser_page):
        browser_page.set_content(
            '<form><input type=file name=resume><div class="g-recaptcha" data-sitekey="app-key"'
            ' style="width:304px;height:78px"></div></form>'
        )
        assert _detect_captcha(browser_page) == {"type": "recaptchav2", "sitekey": "app-key"}

    def test_visible_hcaptcha_is_detected(self, browser_page):
        browser_page.set_content(
            APPLICATION_FORM
            + '<div class="h-captcha" data-sitekey="hc-key" style="width:304px;height:78px"></div>'
        )
        assert _detect_captcha(browser_page) == {"type": "hcaptcha", "sitekey": "hc-key"}


class TestTrapFields:
    def test_oracle_honeypot_gets_no_label(self, browser_page):
        # Honeywell's Oracle page, 2026-09-30
        browser_page.set_content(
            '<div aria-hidden="true"><label for="honey-pot-1">honeypot</label>'
            '<input type="text" name="honey-pot" id="honey-pot-1" tabindex="-1"></div>'
        )
        el = browser_page.query_selector("#honey-pot-1")
        assert _is_trap_field(el)
        assert _get_field_label(browser_page, el) == ""

    def test_workday_beecatcher_is_a_trap(self, browser_page):
        # Curtiss-Wright's Workday sign-in page, 2026-09-30
        browser_page.set_content(
            '<label for="b">Enter website. This input is for robots only, do not enter if '
            "you're human.</label>"
            '<input id="b" name="website" data-automation-id="beecatcher">'
        )
        el = browser_page.query_selector("#b")
        assert _is_trap_field(el)
        assert _get_field_label(browser_page, el) == ""

    def test_leave_blank_label_is_a_trap(self, browser_page):
        browser_page.set_content('<label for="w">Leave this field blank</label><input id="w">')
        el = browser_page.query_selector("#w")
        assert _get_field_label(browser_page, el) == ""

    def test_ordinary_field_keeps_its_label(self, browser_page):
        browser_page.set_content('<label for="c">City</label><input id="c" name="city">')
        el = browser_page.query_selector("#c")
        assert not _is_trap_field(el)
        assert _get_field_label(browser_page, el) == "city"


class TestHttpErrorPage:
    """careers.gov2x.com answered with a bare nginx 403 and the loop spent two
    steps and two AI vision calls on it before failing as "form stuck"."""

    def _check(self, page, html):
        from jobapply.external import _http_error_page

        page.set_content(html)
        return _http_error_page(page)

    def test_nginx_403(self, browser_page):
        html = (
            "<html><head><title>403 Forbidden</title></head><body>"
            "<center><h1>403 Forbidden</h1></center></body></html>"
        )
        assert self._check(browser_page, html) == "403 Forbidden"

    def test_access_denied(self, browser_page):
        html = (
            "<title>Access Denied</title><h1>Access Denied</h1>"
            "<p>You don't have permission to access this server.</p>"
        )
        assert self._check(browser_page, html) == "Access Denied"

    def test_short_page_with_a_form_is_not_an_error(self, browser_page):
        html = "<title>404 help</title><h1>Apply</h1><input name=email>"
        assert self._check(browser_page, html) is None

    def test_ordinary_page_is_not_an_error(self, browser_page):
        assert self._check(browser_page, "<title>Careers</title><h1>Apply now</h1>") is None


class TestHcaptchaCallbackDelivery:
    """Oracle's widget is rendered from script with a private callback; the
    solved token only counts once that callback receives it (2026-09-30)."""

    def test_token_reaches_the_callback_passed_to_render(self, browser_page):
        from urllib.parse import quote

        from jobapply.pages import CAPTCHA_CALLBACK_HOOK_JS, _inject_captcha_token

        browser_page.add_init_script(CAPTCHA_CALLBACK_HOOK_JS)
        site = (
            "<div id=w></div><script>"
            "window.hcaptcha = {render(el, p) { return 'id1'; }};"
            "hcaptcha.render('w', {sitekey: 'k', callback: (t) => { window.gotToken = t; }});"
            "</script>"
        )
        browser_page.goto("data:text/html," + quote(site))
        assert _inject_captcha_token(browser_page, "hcaptcha", "TOKEN-123") is True
        assert browser_page.evaluate("window.gotToken") == "TOKEN-123"
