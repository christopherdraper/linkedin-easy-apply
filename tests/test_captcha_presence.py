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

    def test_leave_blank_label_is_a_trap(self, browser_page):
        browser_page.set_content('<label for="w">Leave this field blank</label><input id="w">')
        el = browser_page.query_selector("#w")
        assert _get_field_label(browser_page, el) == ""

    def test_ordinary_field_keeps_its_label(self, browser_page):
        browser_page.set_content('<label for="c">City</label><input id="c" name="city">')
        el = browser_page.query_selector("#c")
        assert not _is_trap_field(el)
        assert _get_field_label(browser_page, el) == "city"
