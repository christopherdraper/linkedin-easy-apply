"""Workday "Take Assessment" steps that open an outside questionnaire.

Allison Transmission's Workday application has a required "Take Assessment"
step: a Work Opportunity Tax Credit (WOTC) screener run by Maximus
(wotca.tces.maximus.com), opened in a new tab by the step's button. The vision
driver clicked the button but cannot see other tabs, so the step never
completed and every Allison application failed "form stuck" (2026-09-30).

This opens the tab and answers the screener. The questions are about the
employer's tax-credit eligibility (public assistance, veteran status, long-term
unemployment, ...), not about the job. Answers:
- "Are you a resident of <state>?" from the profile's state;
- every other question "I do not wish to answer" where offered (Maximus
  offers it), else "No", which claims no credit on the applicant's behalf;
- optional name/address fields are left blank.
If a page asks for an SSN, a date of birth or any other free-text answer it
requires, nothing is entered and the step is left for the applicant.
"""

import logging
import re
from typing import Optional

log = logging.getLogger("job_apply")

_TAKE_BUTTON = "button:has-text('Take Assessment'), a:has-text('Take Assessment')"
_NEXT = (
    "#MainContent_btnNext, input[type=submit][value='Next'], input[type=submit][value='Submit'], "
    "input[type=submit][value='Finish'], input[type=submit][value='Continue'], "
    "button:has-text('Next'), button:has-text('Submit'), button:has-text('Finish')"
)
_DONE_WORDS = (
    "thank you",
    "has been completed",
    "have completed",
    "you may now close",
    "you may close",
    "questionnaire is complete",
    "survey is complete",
)
_SENSITIVE = re.compile(r"social security|\bssn\b|date of birth|birth ?date|\bdob\b", re.I)
_DECLINE = re.compile(r"do not wish to answer|prefer not|decline", re.I)
_RESIDENT = re.compile(r"resident of the state of ([A-Za-z .]+?)\?", re.I)

_STATE_NAMES = {
    "IN": "Indiana", "CA": "California", "OH": "Ohio", "IL": "Illinois", "MI": "Michigan",
    "KY": "Kentucky", "TX": "Texas", "NY": "New York", "FL": "Florida", "PA": "Pennsylvania",
}  # fmt: skip

_RADIO_GROUPS_JS = """() => {
    const groups = {};
    for (const r of document.querySelectorAll('input[type=radio]')) {
        if (r.getBoundingClientRect().width === 0 && !r.labels?.length) continue;
        const g = groups[r.name] || (groups[r.name] = {name: r.name, checked: false, options: []});
        if (r.checked) g.checked = true;
        const label = r.labels && r.labels[0] ? r.labels[0].innerText.trim() : '';
        g.options.push({id: r.id, value: r.value, label});
        let box = r.closest('fieldset, table, .form-group, div');
        while (box && box.innerText.trim().length < 15 && box.parentElement) box = box.parentElement;
        g.question = box ? box.innerText.trim().slice(0, 300) : '';
    }
    return Object.values(groups);
}"""

_REQUIRED_TEXT_JS = """() => [...document.querySelectorAll(
        'input[type=text], input[type=tel], input[type=date], input:not([type]), textarea, select')]
    .filter(e => e.getBoundingClientRect().width > 0 && !e.value)
    .map(e => {
        const box = e.closest('.form-group, div') || e.parentElement;
        return {label: (e.labels && e.labels[0] ? e.labels[0].innerText : box.innerText).slice(0, 120),
                required: e.required || e.getAttribute('aria-required') === 'true'};
    })"""


def on_assessment_page(page) -> bool:
    """True if the Workday page is a "Take Assessment" step with its button."""
    try:
        heading = page.evaluate(
            "[...document.querySelectorAll('h2,h3')].map(e => e.innerText).join(' ')"
        )
        return "Take Assessment" in heading and page.query_selector(_TAKE_BUTTON) is not None
    except Exception:
        return False


def _choose(group: dict, home_state: str) -> Optional[str]:
    """The radio id to select for one Yes/No question, or None if unsure."""
    opts = {o["label"].strip().lower(): o["id"] for o in group["options"]}
    m = _RESIDENT.search(group.get("question") or "")
    if m:
        want = "yes" if m.group(1).strip().lower() == home_state.lower() else "no"
        return opts.get(want)
    declined = next((i for label, i in opts.items() if _DECLINE.search(label)), None)
    return declined or opts.get("no")


def _fill_page(tab, home_state: str) -> str:
    """Answer what the current page asks. Returns "abort", "answered" or "ready"."""
    english = tab.query_selector("input[value='en'][type=radio]")
    if english and not english.is_checked():
        english.check()  # posts back and reloads the page
        return "answered"
    for field in tab.evaluate(_REQUIRED_TEXT_JS):
        if _SENSITIVE.search(field["label"]) or field["required"]:
            log.info("   Assessment asks for %r; leaving it for the applicant", field["label"][:60])
            return "abort"
    answered = False
    for group in tab.evaluate(_RADIO_GROUPS_JS):
        if group["checked"]:
            continue
        choice = _choose(group, home_state)
        if not choice:
            log.info("   Assessment question with no Yes/No answer: %r", group["question"][:80])
            return "abort"
        tab.check(f"[id='{choice}']")
        label = next(o["label"] for o in group["options"] if o["id"] == choice)
        log.info("   Assessment: %s -> %s", " ".join(group["question"].split())[:70], label)
        answered = True
        tab.wait_for_timeout(1500)  # an answer can post back
    return "answered" if answered else "ready"


def _closed(tab) -> bool:
    if tab.is_closed():
        # Maximus closes its own window after the last answer.
        log.info("   Assessment window closed itself after the last page")
        return True
    return False


def answer_questionnaire(tab, home_state: str, max_pages: int = 40) -> bool:
    """Answer a WOTC-style screener in *tab*. True once it reports completion."""
    last_text = None
    advanced = False
    for _ in range(max_pages):
        try:
            tab.wait_for_timeout(2500)
            text = tab.evaluate("document.body ? document.body.innerText : ''")
            state = _fill_page(tab, home_state)
        except Exception:
            if _closed(tab):
                return advanced
            raise
        if state == "abort":
            return False
        if state == "answered":
            continue  # read the page again before Next
        nxt = tab.query_selector(_NEXT)
        if not nxt:
            done = any(w in text.lower() for w in _DONE_WORDS)
            log.info(
                "   Assessment %s: %r",
                "completed" if done else "ended without a Next button",
                " ".join(text.split())[:160],
            )
            return done
        if text == last_text:
            log.info("   Assessment is not advancing; stopping")
            return False
        last_text = text
        nxt.click()
        advanced = True
        try:
            tab.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception:  # noqa: BLE001
            if _closed(tab):
                return True
    return False


def complete_assessment(page, profile) -> bool:
    """On a Workday "Take Assessment" step, do the outside questionnaire.

    Returns True if a questionnaire was completed (the caller then carries on
    with Save and Continue). A no-op on any other page.
    """
    if not on_assessment_page(page):
        return False
    state = (getattr(profile, "state", "") or "").strip()
    home_state = _STATE_NAMES.get(state.upper(), state)
    log.info("   Workday: opening the Take Assessment questionnaire")
    try:
        with page.context.expect_page(timeout=20000) as new_tab:
            page.click(_TAKE_BUTTON)
        tab = new_tab.value
    except Exception as e:  # noqa: BLE001
        log.info("   Workday: the assessment did not open a tab (%s)", str(e)[:80])
        return False
    try:
        tab.wait_for_load_state("domcontentloaded", timeout=30000)
        return answer_questionnaire(tab, home_state)
    except Exception as e:  # noqa: BLE001
        log.info("   Workday: assessment questionnaire failed: %s", str(e)[:120])
        return False
    finally:
        try:
            tab.close()
        except Exception:  # noqa: BLE001, S110
            pass
