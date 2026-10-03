"""Education dates are withheld on purpose (applicant choice, 2026-10-03).

TailorCare's Greenhouse form required "Start date month*" and the AI guessed
months. Without dates in the profile: optional education date fields stay
blank, and a required one ends the application as a flagged skip.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobapply.forms import (  # noqa: E402
    EDUCATION_DATES_SKIP_STATUS,
    _enforce_education_date_policy,
    _get_field_label,
)
from jobapply.safety import ApplicantPolicySkip, ApplicationAbortError  # noqa: E402

NO_DATES = SimpleNamespace(education_year=None)


def education(required_start: bool) -> str:
    star = "*" if required_start else ""
    req = "aria-required='true'" if required_start else ""
    return f"""
<fieldset><legend>Education</legend>
  <label for="school--0">School*</label><input id="school--0" aria-required="true">
  <label for="start-month--0">Start date month{star}</label>
  <input id="start-month--0" role="combobox" {req}>
  <label for="end-year--0">End date year</label><input id="end-year--0">
</fieldset>
<label for="avail">Earliest start date*</label><input id="avail" required>"""


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


def test_required_education_date_skips_with_flagged_status(browser_page):
    browser_page.set_content(education(required_start=True))
    with pytest.raises(ApplicantPolicySkip) as exc:
        _enforce_education_date_policy(browser_page, NO_DATES)
    assert exc.value.status == EDUCATION_DATES_SKIP_STATUS == "skipped: requires education dates"
    # Still an abort for every existing except-ApplicationAbortError path.
    assert isinstance(exc.value, ApplicationAbortError)


def test_optional_dates_left_blank_other_fields_untouched(browser_page):
    browser_page.set_content(education(required_start=False))
    _enforce_education_date_policy(browser_page, NO_DATES)  # no skip
    label = lambda sel: _get_field_label(browser_page, browser_page.query_selector(sel))  # noqa: E731
    assert label("#start-month--0") == ""  # fillers skip it
    assert label("#end-year--0") == ""
    assert label("#school--0") == "school*"
    # Availability date is not an education date.
    assert label("#avail") == "earliest start date*"


def test_profile_with_dates_is_not_restricted(browser_page):
    browser_page.set_content(education(required_start=True))
    _enforce_education_date_policy(browser_page, SimpleNamespace(education_year=2014))
    assert _get_field_label(browser_page, browser_page.query_selector("#start-month--0"))
