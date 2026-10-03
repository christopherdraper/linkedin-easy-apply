"""Dover form handling, LinkedIn's share-profile prompt, Turnstile sitekeys.

All three surfaced on 2026-10-03 applying to Kintsugi (Dover): the LinkedIn
Apply click opened a "Share your profile?" dialog the bot never continued,
Dover's MUI fields have no <label> so nothing was filled, and its Turnstile
widget is drawn in a closed shadow root. Verified live: submitted.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ats_handlers import get_handler  # noqa: E402
from ats_handlers.dover import DoverHandler  # noqa: E402
from jobapply.external import _continue_share_profile_prompt  # noqa: E402
from jobapply.pages import _turnstile_sitekey_from_frames  # noqa: E402

DOVER_FORM = """
<form novalidate>
  <div role="button"><input type="file" accept=".pdf" style="display:none">
    <div>Autofill from resume</div></div>
  <div><div class="styles__FormLabel-x">First Name *</div>
    <div class="MuiFormControl-root"><div><input id=":r2:" name="firstName" required></div></div></div>
  <div><div class="styles__FormLabel-x">Last Name *</div>
    <div class="MuiFormControl-root"><div><input id=":r3:" name="lastName" required></div></div></div>
  <div><div class="styles__FormLabel-x">Email *</div>
    <div><div><input id=":r4:" name="email" type="email" required></div></div></div>
  <div><div class="styles__FormLabel-x">Why Kintsugi?</div>
    <div><div><textarea id=":r7:"></textarea></div></div></div>
  <div><div class="styles__FormLabel-x">Resume *</div>
    <div role="button"><input type="file" accept=".pdf" style="display:none"></div></div>
  <button type="submit">Apply</button>
</form>"""


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


def _profile(tmp_path):
    (tmp_path / "Resume.pdf").write_bytes(b"%PDF-1.4\n")
    return SimpleNamespace(
        full_name="Christopher Draper",
        email="c@example.com",
        linkedin_url="https://www.linkedin.com/in/x",
        phone="+1-555-0100",
        resume_path=str(tmp_path / "Resume.docx"),
    )


def test_dover_urls_route_to_the_handler():
    assert get_handler("https://app.dover.com/apply/Kintsugi%20AI/abc").platform_name == "Dover"


class TestDoverForm:
    def test_fills_contacts_labels_questions_and_uploads_pdf(self, browser_page, tmp_path):
        browser_page.set_content(DOVER_FORM)
        DoverHandler().on_step_start(browser_page, {"profile": _profile(tmp_path)})

        value = lambda name: browser_page.input_value(f"input[name='{name}']")  # noqa: E731
        assert (value("firstName"), value("lastName"), value("email")) == (
            "Christopher",
            "Draper",
            "c@example.com",
        )
        # Custom question gets its caption so the generic filler can answer it.
        assert browser_page.get_attribute("textarea", "aria-label") == "Why Kintsugi?"
        files = browser_page.query_selector_all("input[type=file]")
        # Autofill input fenced off and empty; PDF (not the .docx) in Resume.
        assert files[0].get_attribute("data-jobapply-skip") == "dover-autofill"
        assert files[0].evaluate("el => el.files.length") == 0
        assert files[1].evaluate("el => el.files[0].name") == "Resume.pdf"

    def test_thanks_card_is_success_but_the_form_is_not(self, browser_page):
        handler = DoverHandler()
        browser_page.set_content(DOVER_FORM)
        assert handler.detect_success(browser_page, {}) is False
        browser_page.set_content("<div><h2>Thanks for applying!</h2><a>View Jobs</a></div>")
        assert handler.detect_success(browser_page, {}) is True


def test_share_profile_prompt_continue_resolves_the_ats_url():
    link = MagicMock()
    link.get_attribute.return_value = "https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fapp%2Edover%2Ecom%2Fapply%2Fx&urlhash=1"
    page = MagicMock()
    page.query_selector.return_value = link
    assert _continue_share_profile_prompt(page) == "https://app.dover.com/apply/x"
    link.click.assert_called()  # clicked, so the applicant's share choice applies

    page.query_selector.return_value = None
    assert _continue_share_profile_prompt(page) == ""


def test_turnstile_sitekey_read_from_challenge_frame_url():
    page = MagicMock()
    page.frames = [
        SimpleNamespace(url="https://app.dover.com/apply/x"),
        SimpleNamespace(
            url="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/"
            "f/av0/rch/uqqls/0x4AAAAAADKUkt7PjrxKem2G/light/fbE/new/flexible?lang=auto"
        ),
    ]
    assert _turnstile_sitekey_from_frames(page) == "0x4AAAAAADKUkt7PjrxKem2G"
    page.frames = []
    assert _turnstile_sitekey_from_frames(page) == ""
