"""LinkedIn's 2026 Easy Apply city typeahead (Cribl, 2026-10-03).

The caption is a <p> and the field only validates once a suggestion is picked;
the old typeahead selectors matched nothing, so Next never advanced.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobapply.easy_apply import _fill_location_typeaheads  # noqa: E402

MODAL = """
<div role="dialog">
  <div componentkey="easyApplyFieldFocus_ea.q::1::UNKNOWN::value.validation">
    <p>Location (city)*</p>
    <input data-testid="typeahead-input" placeholder="Enter city or location" value="">
  </div>
</div>
<div data-testid="typeahead-results-container" role="listbox"></div>
<script>
  const inp = document.querySelector('input');
  const box = document.querySelector('[role=listbox]');
  inp.addEventListener('input', () => {
    box.innerHTML = ['Greater Indianapolis', 'Indianapolis, Indiana, United States']
      .map(t => `<div role="option"><div role="button"><p>${t}</p></div></div>`).join('');
    box.querySelectorAll('[role=option]').forEach(o =>
      o.addEventListener('click', () => { inp.value = o.innerText; box.innerHTML = ''; }));
  });
</script>"""


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


def test_city_typed_and_city_suggestion_picked_over_metro(browser_page):
    browser_page.set_content(MODAL)
    assert _fill_location_typeaheads(browser_page, SimpleNamespace(city="Indianapolis")) == 1
    assert browser_page.input_value("input") == "Indianapolis, Indiana, United States"


def test_filled_or_cityless_profiles_are_left_alone(browser_page):
    browser_page.set_content(MODAL)
    assert _fill_location_typeaheads(browser_page, SimpleNamespace(city=None)) == 0
    browser_page.fill("input", "Chicago")
    assert _fill_location_typeaheads(browser_page, SimpleNamespace(city="Indianapolis")) == 0
