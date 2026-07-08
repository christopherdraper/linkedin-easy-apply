"""Lever ATS handler.

Lever was 0% success (0/7). Live debugging on jobs.lever.co (2026-07-08)
found the real blockers and this handler fixes them:

1. **Labels are ancestor <label> wrappers.** Lever renders
   ``<label><div class="application-label">Full name</div>
   <div class="application-field"><input name="name"></div></label>``.
   The generic ``_get_field_label`` never checks ancestor labels, so every
   text input was skipped ("form not progressing (no new fields filled)").
   ``on_step_start`` pre-fills all fixed-name Lever fields (name, email,
   phone, org, urls[...], location) straight from the profile.

2. **Yes/No question "cards" are checkbox PAIRS that are both
   ``required``.** The generic mandatory-checkbox pass ticks the first
   unchecked required box, i.e. answers "Yes" to "Will you require visa
   sponsorship?". The handler answers cards from screening_answers (AI
   fallback) and strips ``required`` from the unchosen siblings so neither
   the generic pass nor HTML5 validation trips over them.

3. **hCaptcha (sitekey in static HTML, solved fine by 2captcha).** Lever's
   inline JS gates the real submit on ``#hcaptchaResponseInput`` (hidden
   input, name="h-captcha-response") being non-empty. The shared injector's
   ``querySelector('[name="h-captcha-response"]')`` happens to hit that
   hidden input first in DOM order, but ``_relay_hcaptcha_token`` copies the
   token from the widget textarea defensively in case another posting's DOM
   order differs. Historical "captcha solve failed" entries were 2captcha
   transient failures, not a structural problem (verified solving live).

4. **EEO / demographic sections.** EEO selects (``eeo[gender]`` etc.) are
   answered deterministically from screening_answers instead of the generic
   AI pass (which live-answered "3"). Voluntary ``surveysResponses`` groups
   with no profile answer get their decline option when one exists,
   otherwise their inputs are disabled so the generic radio pass can't
   misreport demographics (disabled+unchecked inputs are never submitted).

Prior attempt (reverted 2026-04-24): cookie-banner dismissal in pre_flight.
Live inspection showed the banner never blocked the form; not re-added.
"""

import logging
import re
from datetime import datetime
from typing import Optional

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register

log = logging.getLogger("job_apply")

# Posting page: https://jobs.lever.co/<company>/<uuid>  (optionally jobs.eu.lever.co)
# The /apply suffix is the actual form page.
_POSTING_URL_RE = re.compile(
    r"^(https://jobs\.(?:eu\.)?lever\.co/[^/?#]+/[0-9a-fA-F-]{36})/?(\?[^#]*)?$"
)

# Voluntary-survey options that mean "decline to answer"
_DECLINE_PATTERNS = ("decline", "don't wish", "do not wish", "prefer not", "rather not")

# EEO select name -> screening_answers keys (first hit wins)
_EEO_SELECT_KEYS = {
    "eeo[gender]": ("gender", "sex"),
    "eeo[race]": ("race", "ethnicity"),
    "eeo[veteran]": ("veteran status", "veteran"),
    "eeo[disability]": ("disability status", "disability"),
}

# Demographic survey question keywords -> screening_answers keys
_SURVEY_KEYWORD_KEYS = (
    ("gender", ("gender", "sex")),
    ("race", ("race", "ethnicity")),
    ("ethnic", ("race", "ethnicity")),
    ("veteran", ("veteran status", "veteran")),
    ("disab", ("disability status", "disability")),
)


class LeverHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Lever"

    # ------------------------------------------------------------------
    # Q1 hooks
    # ------------------------------------------------------------------

    def pre_flight(self, page, ctx: dict) -> Optional[str]:
        """Jump from the posting page straight to the /apply form page.

        The posting page's "Apply for this job" button is just a link to
        ``<posting>/apply``; navigating directly skips a classify/click
        round-trip and puts the form in front of the fill loop immediately.
        """
        self._goto_apply_page(page)
        return None

    def q2_pre_flight(self, page, ctx: dict) -> Optional[str]:
        self._goto_apply_page(page)
        return None

    def on_step_start(self, page, ctx: dict) -> Optional[str]:
        """Pre-fill every Lever-specific field the generic loop can't reach.

        Runs before the generic fill phase each iteration; all helpers are
        idempotent (skip already-filled fields), so repeat iterations no-op.
        """
        profile = ctx.get("profile") if isinstance(ctx, dict) else None
        try:
            has_form = page.query_selector("#application-form") is not None
        except Exception:  # noqa: BLE001
            has_form = False
        if profile and has_form:
            filled = 0
            filled += self._fill_contact_fields(page, profile)
            filled += self._fill_location(page, profile)
            filled += self._select_posting_location(page, profile)
            filled += self._answer_question_cards(page, profile)
            filled += self._fill_eeo_selects(page, profile)
            self._resolve_demographic_surveys(page, profile)
            if filled:
                log.info("   Lever: pre-filled %d Lever-specific fields", filled)
        self._relay_hcaptcha_token(page)
        return None

    def on_submit_clicked(self, page, ctx: dict) -> Optional[str]:
        """Recover when the submit click stalled on a missing hCaptcha token.

        Lever's #btn-submit listener only forwards to the real (hidden)
        submit button when #hcaptchaResponseInput has a token; otherwise it
        fires hcaptcha.execute() and pops a challenge. If we're still on the
        /apply page with an empty token input, relay the token and re-click.
        """
        try:
            if "/apply" not in (page.url or ""):
                return None
            if self._relay_hcaptcha_token(page):
                btn = page.query_selector("#btn-submit")
                if btn and btn.is_visible():
                    from job_search_apply import _safe_click

                    _safe_click(btn, page)
                    page.wait_for_timeout(3000)
        except Exception as e:  # noqa: BLE001
            log.debug("Lever on_submit_clicked recovery failed: %s", e)
        return None

    def detect_success(self, page, ctx: dict) -> bool:
        """Lever redirects to <posting>/thanks after a successful POST."""
        try:
            url = (page.url or "").lower()
            if "lever.co" in url and "/thanks" in url:
                return True
            body = page.evaluate(
                "() => (document.body ? document.body.innerText : '').toLowerCase()"
            )
            return "application submitted" in body and "submit application" not in body
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _goto_apply_page(page) -> None:
        m = _POSTING_URL_RE.match(page.url or "")
        if not m:
            return
        apply_url = m.group(1) + "/apply" + (m.group(2) or "")
        try:
            log.info("   Lever: navigating directly to apply form: %s", apply_url[:100])
            page.goto(apply_url, timeout=30000)
            page.wait_for_timeout(1500)
        except Exception as e:  # noqa: BLE001
            log.debug("Lever apply-page jump failed: %s", e)

    @staticmethod
    def _record_fill(field: str, value: str) -> None:
        """Append to the shared per-application fill log (dashboard report)."""
        try:
            from jobapply import stats

            stats._field_fills.append(
                {"field": field[:100], "value": value[:100], "source": "lever_handler"}
            )
        except Exception:  # noqa: BLE001, S110
            pass

    def _fill_contact_fields(self, page, profile) -> int:
        """Fill Lever's fixed-name contact inputs directly from the profile.

        Generic label lookup fails on Lever (ancestor-<label> wrapping), so
        target the stable name attributes instead.
        """
        mapping = [
            ("input[name='name']", "Full name", profile.full_name),
            ("input[name='email']", "Email", profile.email),
            ("input[name='phone']", "Phone", profile.phone),
            ("input[name='org']", "Current company", profile.current_employer),
            ("input[name='urls[LinkedIn]']", "LinkedIn URL", profile.linkedin_url),
            ("input[name='urls[GitHub]']", "GitHub URL", profile.github_url),
        ]
        filled = 0
        for sel, label, value in mapping:
            if not value:
                continue
            try:
                inp = page.query_selector(sel)
                if inp and inp.is_visible() and not inp.input_value():
                    inp.fill(str(value))
                    filled += 1
                    self._record_fill(label, str(value))
                    log.info("   Lever: filled %s", label)
            except Exception as e:  # noqa: BLE001
                log.debug("Lever contact fill failed for %s: %s", sel, e)
        return filled

    def _fill_location(self, page, profile) -> int:
        """Type-and-select the current-location typeahead.

        Picking a suggestion also sets the hidden ``selectedLocation``
        payload; plain typed text is still accepted by Lever if no
        suggestion appears, so fall back to leaving the text in place.
        """
        if not profile.city:
            return 0
        try:
            inp = page.query_selector("#location-input, input[name='location']")
            if not inp or not inp.is_visible() or inp.input_value():
                return 0
            text = ", ".join(x for x in (profile.city, profile.state) if x)
            inp.click()
            inp.type(text, delay=40)
            # Suggestions load async; Lever CLEARS free text on blur when no
            # suggestion was picked, so poll a few times before giving up.
            picked = None
            for _ in range(3):
                page.wait_for_timeout(1300)
                picked = page.evaluate("""() => {
                    const results = document.querySelectorAll(
                        '.dropdown-container .dropdown-results > *');
                    for (const r of results) {
                        const rect = r.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        const t = (r.innerText || '').trim();
                        if (!t) continue;
                        r.click();
                        return t.slice(0, 80);
                    }
                    return null;
                }""")
                if picked:
                    break
            if picked:
                log.info("   Lever: location suggestion picked -> %s", picked)
            else:
                # Free text is accepted; just close the dropdown.
                page.keyboard.press("Escape")
                log.info("   Lever: location left as typed text -> %s", text)
            self._record_fill("Current location", picked or text)
            return 1
        except Exception as e:  # noqa: BLE001
            log.debug("Lever location fill failed: %s", e)
            return 0

    def _select_posting_location(self, page, profile) -> int:
        """Answer the "which location are you applying for?" select.

        Prefer the profile city/state, then any Remote option, then the
        first real option (multi-location postings require a choice).
        """
        try:
            sel = page.query_selector("select[name='opportunityLocationId']")
            if not sel or not sel.is_visible():
                return 0
            if (sel.input_value() or "").strip():
                return 0
            options = sel.query_selector_all("option")
            texts = [
                (o.get_attribute("value") or "", (o.inner_text() or "").strip()) for o in options
            ]
            real = [(v, t) for v, t in texts if v]
            if not real:
                return 0
            choice = None
            for want in (profile.city or "", profile.state or "", "remote"):
                if not want:
                    continue
                for v, t in real:
                    if want.lower() in t.lower():
                        choice = (v, t)
                        break
                if choice:
                    break
            if not choice:
                choice = real[0]
            sel.select_option(value=choice[0])
            self._record_fill("Which location are you applying for?", choice[1])
            log.info("   Lever: posting location -> %s", choice[1])
            return 1
        except Exception as e:  # noqa: BLE001
            log.debug("Lever posting-location select failed: %s", e)
            return 0

    def _answer_question_cards(self, page, profile) -> int:  # noqa: C901
        """Answer custom question "cards" (checkbox/radio groups, selects,
        text inputs) and strip ``required`` from unchosen checkbox siblings.

        Lever renders Yes/No questions as checkbox PAIRS where each box is
        ``required``; the generic mandatory-checkbox pass would tick the
        first one ("Yes" to visa sponsorship). Answer from screening_answers
        (AI fallback), then remove ``required`` from the rest of the group
        so neither the generic pass nor HTML5 validation touches them.
        """
        from job_search_apply import (
            _ai_answer_question,
            _best_option_match,
            _match_screening_answer,
        )

        filled = 0
        # --- checkbox / radio card groups, grouped by name attribute ---
        try:
            toggles = page.query_selector_all(
                "input[name^='cards['][type='checkbox'], input[name^='cards['][type='radio']"
            )
        except Exception:  # noqa: BLE001
            toggles = []
        groups: dict = {}
        for el in toggles:
            try:
                groups.setdefault(el.get_attribute("name") or "", []).append(el)
            except Exception:  # noqa: BLE001, S112
                continue
        for name, els in groups.items():
            if not name:
                continue
            try:
                if any(e.is_checked() for e in els):
                    self._strip_required_from_unchecked(els)
                    continue
                question = self._card_question_text(els[0])
                values = [(e.get_attribute("value") or "").strip() for e in els]
                answer = _match_screening_answer(question.lower(), profile.screening_answers)
                if not answer:
                    answer = _ai_answer_question(
                        f"{question} (options: {', '.join(values)})", profile
                    )
                if not answer:
                    continue
                idx = _best_option_match(answer, values)
                if idx < 0:
                    log.debug("Lever card %r: no option match for %r", question[:60], answer)
                    continue
                els[idx].check(force=True)
                page.wait_for_timeout(150)
                self._strip_required_from_unchecked(els)
                filled += 1
                self._record_fill(question, values[idx])
                log.info("   Lever: card %r -> %s", question[:60], values[idx])
            except Exception as e:  # noqa: BLE001
                log.debug("Lever card group %r failed: %s", name[:40], e)

        # --- card selects ---
        try:
            card_selects = page.query_selector_all("select[name^='cards[']")
        except Exception:  # noqa: BLE001
            card_selects = []
        for sel in card_selects:
            try:
                if (sel.input_value() or "").strip():
                    continue
                question = self._card_question_text(sel)
                opts = sel.query_selector_all("option")
                pairs = [
                    (o.get_attribute("value") or "", (o.inner_text() or "").strip()) for o in opts
                ]
                real = [(v, t) for v, t in pairs if v]
                if not real:
                    continue
                answer = _match_screening_answer(
                    question.lower(), profile.screening_answers
                ) or _ai_answer_question(
                    f"{question} (options: {', '.join(t for _, t in real)})", profile
                )
                if not answer:
                    continue
                idx = _best_option_match(answer, [t for _, t in real])
                if idx < 0:
                    continue
                sel.select_option(value=real[idx][0])
                filled += 1
                self._record_fill(question, real[idx][1])
                log.info("   Lever: card select %r -> %s", question[:60], real[idx][1])
            except Exception as e:  # noqa: BLE001
                log.debug("Lever card select failed: %s", e)

        # --- card text inputs / textareas (generic label lookup fails here) ---
        try:
            texts = page.query_selector_all(
                "input[name^='cards['][type='text'], textarea[name^='cards[']"
            )
        except Exception:  # noqa: BLE001
            texts = []
        for inp in texts:
            try:
                if not inp.is_visible() or inp.input_value():
                    continue
                question = self._card_question_text(inp)
                required = inp.get_attribute("required") is not None
                answer = _match_screening_answer(question.lower(), profile.screening_answers)
                if not answer and required:
                    answer = _ai_answer_question(question, profile)
                if not answer:
                    continue
                inp.fill(str(answer))
                filled += 1
                self._record_fill(question, str(answer))
                log.info("   Lever: card text %r -> %s", question[:60], str(answer)[:40])
            except Exception as e:  # noqa: BLE001
                log.debug("Lever card text failed: %s", e)
        return filled

    @staticmethod
    def _card_question_text(el) -> str:
        try:
            text = el.evaluate("""el => {
                const li = el.closest('li.application-question, .application-question');
                const lbl = li && li.querySelector('.application-label');
                return lbl ? lbl.innerText : '';
            }""")
            return " ".join((text or "").replace("✱", " ").split())
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _strip_required_from_unchecked(els) -> None:
        """Remove ``required`` from unchecked toggles in an answered group.

        Mirrors Lever's own client-side validation behavior and stops the
        generic mandatory-checkbox pass from ticking the sibling box.
        """
        for e in els:
            try:
                if not e.is_checked():
                    e.evaluate("el => el.removeAttribute('required')")
            except Exception:  # noqa: BLE001, S112
                continue

    def _fill_eeo_selects(self, page, profile) -> int:
        """Answer EEO selects deterministically from screening_answers.

        The generic select pass answers these via AI with garbage (live run
        answered "3"). If the disability question got answered, also fill
        the signature (full name) and date inputs the OFCCP form requires.
        """
        from job_search_apply import _best_option_match

        filled = 0
        answered_disability = False
        for name, keys in _EEO_SELECT_KEYS.items():
            try:
                sel = page.query_selector(f"select[name='{name}']")
                if not sel or not sel.is_visible():
                    continue
                if (sel.input_value() or "").strip():
                    continue
                answer = next(
                    (profile.screening_answers[k] for k in keys if k in profile.screening_answers),
                    None,
                )
                if not answer:
                    continue
                opts = sel.query_selector_all("option")
                pairs = [
                    (o.get_attribute("value") or "", (o.inner_text() or "").strip()) for o in opts
                ]
                real = [(v, t) for v, t in pairs if v]
                idx = _best_option_match(str(answer), [t for _, t in real])
                if idx < 0:
                    continue
                sel.select_option(value=real[idx][0])
                filled += 1
                if name == "eeo[disability]":
                    answered_disability = True
                self._record_fill(name, real[idx][1])
                log.info("   Lever: %s -> %s", name, real[idx][1])
            except Exception as e:  # noqa: BLE001
                log.debug("Lever EEO select %s failed: %s", name, e)

        if answered_disability:
            try:
                sig = page.query_selector("input[name='eeo[disabilitySignature]']")
                if sig and sig.is_visible() and not sig.input_value():
                    sig.fill(profile.full_name)
                    filled += 1
                    self._record_fill("Disability signature", profile.full_name)
                date = page.query_selector("input[name='eeo[disabilitySignatureDate]']")
                if date and date.is_visible() and not date.input_value():
                    today = datetime.now().strftime("%m/%d/%Y")
                    date.fill(today)
                    filled += 1
                    self._record_fill("Disability signature date", today)
            except Exception as e:  # noqa: BLE001
                log.debug("Lever disability signature failed: %s", e)
        return filled

    def _resolve_demographic_surveys(self, page, profile) -> None:
        """Handle voluntary ``surveysResponses`` demographic groups.

        Answer from the profile when we have the demographic on file, pick
        the decline option when one exists, otherwise DISABLE the group's
        inputs: they're voluntary, and leaving them enabled lets the generic
        radio pass misreport demographics (live run clicked an age radio).
        Disabled unchecked inputs are never submitted, so this equals
        leaving the section blank.
        """
        try:
            toggles = page.query_selector_all(
                "input[name^='surveysResponses['][type='radio'], "
                "input[name^='surveysResponses['][type='checkbox']"
            )
        except Exception:  # noqa: BLE001
            return
        groups: dict = {}
        for el in toggles:
            try:
                groups.setdefault(el.get_attribute("name") or "", []).append(el)
            except Exception:  # noqa: BLE001, S112
                continue
        for name, els in groups.items():
            if not name:
                continue
            try:
                if any(e.is_checked() for e in els):
                    continue
                # Already neutralized on a previous iteration
                if all(e.is_disabled() for e in els):
                    continue
                question = self._card_question_text(els[0]).lower()
                values = [(e.get_attribute("value") or "").strip() for e in els]
                answer = None
                for kw, keys in _SURVEY_KEYWORD_KEYS:
                    if kw in question:
                        answer = next(
                            (
                                profile.screening_answers[k]
                                for k in keys
                                if k in profile.screening_answers
                            ),
                            None,
                        )
                        break
                idx = -1
                if answer:
                    from job_search_apply import _best_option_match

                    idx = _best_option_match(str(answer), values)
                if idx < 0:
                    idx = next(
                        (
                            i
                            for i, v in enumerate(values)
                            if any(p in v.lower() for p in _DECLINE_PATTERNS)
                        ),
                        -1,
                    )
                if idx >= 0:
                    els[idx].check(force=True)
                    self._record_fill(question or name, values[idx])
                    log.info("   Lever: survey %r -> %s", (question or name)[:60], values[idx])
                else:
                    for e in els:
                        e.evaluate("el => { el.disabled = true; }")
                    log.info(
                        "   Lever: survey %r left blank (voluntary, no profile answer)",
                        (question or name)[:60],
                    )
            except Exception as e:  # noqa: BLE001
                log.debug("Lever survey group %r failed: %s", name[:40], e)

    @staticmethod
    def _relay_hcaptcha_token(page) -> bool:
        """Copy a solved hCaptcha token into Lever's hidden response input.

        Lever's submit-button listener requires #hcaptchaResponseInput to be
        non-empty; the shared injector usually hits it first by DOM order,
        but relay from the widget textarea in case it didn't.
        """
        try:
            return bool(
                page.evaluate("""() => {
                    const hidden = document.getElementById('hcaptchaResponseInput');
                    if (!hidden || hidden.value) return false;
                    const ta = document.querySelector(
                        '#h-captcha textarea[name="h-captcha-response"], '
                        + 'textarea[name="h-captcha-response"]');
                    if (ta && ta.value) { hidden.value = ta.value; return true; }
                    return false;
                }""")
            )
        except Exception:  # noqa: BLE001
            return False


register("Lever", LeverHandler)
