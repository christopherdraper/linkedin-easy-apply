"""Workday "Take Assessment" questionnaires (Allison's Maximus WOTC screener,
2026-09-30), driven in a real browser against a stand-in screener."""

import sys
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ats_handlers._workday_assessment import _choose, answer_questionnaire  # noqa: E402

SCREENER = """
<div id=app></div>
<script>
const pages = [
  '<label><input type=radio name=lang value=en id=l1>English</label>'
  + '<label><input type=radio name=lang value=es id=l2>Espanol</label>',
  '<p>Are you a resident of the State of Indiana?</p>'
  + '<label><input type=radio name=q1 id=a1>Yes</label><label><input type=radio name=q1 id=a2>No</label>',
  '<p>Have you received SNAP benefits?</p>'
  + '<label><input type=radio name=q2 id=b1>Yes</label><label><input type=radio name=q2 id=b2>No</label>'
  + '<label><input type=radio name=q2 id=b3>I do not wish to answer</label>',
  EXTRA,
  '<p>Thank you. Your questionnaire has been completed.</p>',
];
window.answers = {};
let i = 0;
function show() {
  document.getElementById('app').innerHTML = pages[i]
    + (i < pages.length - 1 ? '<input type=submit id=MainContent_btnNext value=Next>' : '');
  const next = document.getElementById('MainContent_btnNext');
  if (next) next.onclick = () => {
    document.querySelectorAll('input[type=radio]:checked').forEach(r => window.answers[r.name] = r.id);
    i++; show();
  };
}
show();
</script>
"""


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        pw = sync_api.sync_playwright().start()
        b = pw.chromium.launch()
    except Exception as exc:  # no browser binaries (CI)
        pytest.skip(f"headless Chromium unavailable: {exc}")
    yield b
    b.close()
    pw.stop()


def _open(browser, extra_page):
    tab = browser.new_page()
    html = SCREENER.replace("EXTRA", repr(extra_page))
    tab.goto("data:text/html," + quote(html))
    return tab


def test_screener_is_answered_and_completed(browser):
    tab = _open(
        browser,
        "'<p>Are you a veteran?</p>"
        "<label><input type=radio name=q3 id=c1>Yes</label>"
        "<label><input type=radio name=q3 id=c2>No</label>'",
    )
    assert answer_questionnaire(tab, "Indiana") is True
    answers = tab.evaluate("window.answers")
    assert answers == {"lang": "l1", "q1": "a1", "q2": "b3", "q3": "c2"}


def test_ssn_request_is_left_for_the_applicant(browser):
    tab = _open(browser, "'<label for=s>Social Security Number</label><input id=s type=text>'")
    assert answer_questionnaire(tab, "Indiana") is False
    assert tab.evaluate("document.getElementById('s').value") == ""


def test_residency_follows_the_profile_state():
    group = {
        "question": "Are you a resident of the State of California?",
        "options": [{"id": "y", "label": "Yes"}, {"id": "n", "label": "No"}],
    }
    assert _choose(group, "Indiana") == "n"
    assert _choose(group, "California") == "y"
