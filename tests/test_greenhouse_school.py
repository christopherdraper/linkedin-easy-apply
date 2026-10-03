"""Greenhouse education School* picker (Voxel51, TailorCare, 2026-10-03).

The searchable select shows only the alphabetical head of its school list, so
the applicant's school had to be typed to appear; unfilled, the required
field failed validation until the step budget ran out.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ats_handlers.greenhouse import GreenhouseHandler  # noqa: E402

# Minimal react-select shape as Greenhouse renders it.
SELECT = """
<div class="select__control"><div class="select__value-container">
  <div class="select__input-container"><input id="school--0" role="combobox"></div>
</div></div>
<div id="react-select-school--0-listbox" role="listbox"></div>
<script>
  const inp = document.querySelector('#school--0');
  const box = document.querySelector('[role=listbox]');
  const all = ['Aalborg University', 'Ball State University', 'Ball State Teachers College'];
  inp.addEventListener('input', () => {
    box.innerHTML = all.filter(s => s.toLowerCase().includes(inp.value.toLowerCase()))
      .map(s => `<div role="option">${s}</div>`).join('');
    box.querySelectorAll('[role=option]').forEach(o => o.addEventListener('click', () => {
      document.querySelector('.select__value-container').insertAdjacentHTML(
        'afterbegin', `<div class="select__single-value">${o.innerText}</div>`);
      inp.value = ''; box.innerHTML = '';
    }));
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


def test_school_typed_exact_option_picked_once(browser_page):
    browser_page.set_content(SELECT)
    profile = SimpleNamespace(education_university="Ball State University")
    assert GreenhouseHandler._fill_schools(browser_page, profile) == 1
    shown = browser_page.inner_text(".select__single-value")
    assert shown == "Ball State University"
    # Already chosen: a later step does not pick it again.
    assert GreenhouseHandler._fill_schools(browser_page, profile) == 0


def test_school_not_in_list_is_left_for_the_applicant(browser_page):
    browser_page.set_content(SELECT)
    profile = SimpleNamespace(education_university="Purdue University")
    assert GreenhouseHandler._fill_schools(browser_page, profile) == 0


CHOSEN = """
<label for="school--0">School*</label>
<div class="select__control"><div class="select__value-container">
  <div class="select__single-value">Ball State University</div>
  <div class="select__input-container"><input id="school--0" role="combobox"></div>
</div>
<button type="button" aria-label="Clear selections" data-testid="clear-selection"
  onclick="document.querySelector('.select__single-value').remove()"></button></div>"""


def test_div_select_pass_never_clicks_clear_selection(browser_page):
    # The "x" matched [data-testid*='select'], looked like an empty select,
    # and was clicked, wiping the chosen school.
    from jobapply.external import _answer_div_custom_selects

    browser_page.set_content(CHOSEN)
    _answer_div_custom_selects(browser_page, SimpleNamespace())
    assert browser_page.inner_text(".select__single-value") == "Ball State University"
