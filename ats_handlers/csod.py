"""Cornerstone OnDemand (CSOD) ATS handler.

Tenants are ``<company>.csod.com`` and the application lives at
``/ux/ats/careersite/<n>/requisition/<id>/application`` as a short wizard
("Step 1 of 2").

Why this handler exists: the generic filler cannot see CSOD's contact fields.
First name, last name and email carry an EMPTY ``aria-label`` and no
``<label for>`` -- their only identity is
``aria-labelledby="actionItem.firstName.idTag-error"``. The generic label
lookup found nothing, so the AI fell back to guessing against section
headings ("contact information -- choose one" answered with a field label),
every step filled zero real fields, and the run stalled at the stall-detector
having completed none of the required inputs.

Everything else on the page is addressable deterministically -- the remaining
text inputs use plain ``aria-label`` and the EEO questions are selects with
``EEOQuestion-<n>`` ids -- so this handler owns the whole form and keeps the
generic (guessing) filler off it via ``ctx["skip_step"]``.
"""

import logging

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register

log = logging.getLogger("job_apply")

# Per-step cap on advance attempts, so a step that never validates fails the
# application instead of clicking Next forever.
_CSOD_MAX_ADVANCES = 4

# CSOD's résumé parser can emit dozens of part-filled Skills / Certifications
# rows, so the prune bound has to clear a realistic worst case while still
# terminating.
_CSOD_MAX_PRUNES = 60

# Option text CSOD offers for declining a voluntary question.
_DECLINE_OPTION = "i do not want to answer"

# Procedural acknowledgements: acknowledging receipt of a privacy notice, or
# consenting to the handling of the data being submitted, is inherent to
# lodging the application at all -- Linde blocks Submit until they are "Yes".
# These are not substantive claims about the candidate, unlike the screening
# questions, which stay with the profile-driven resolver.
_CONSENT_PATTERNS = (
    "privacy notice",
    "acknowledge receipt",
    "i acknowledge",
    "i consent",
    "consent to",
    "i hereby confirm",
    "terms and conditions",
    "i agree",
)

# Profile values that mean "decline to answer".
_DECLINE_VALUES = ("prefer not", "decline", "do not wish", "do not want", "not disclose")


class CornerstoneHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Cornerstone"

    def pre_flight(self, page, ctx):
        self._dismiss_cookie_banner(page)
        return None

    def on_step_start(self, page, ctx):
        self._dismiss_cookie_banner(page)

        if self._is_confirmation(page):
            log.info("   Cornerstone: application confirmed")
            return "submitted"

        profile = ctx.get("profile")
        if profile is None or not self._is_application_form(page):
            return None  # login/landing page -- let the generic logic handle it

        step = self._step_key(page)
        if ctx.get("_csod_filled") != step:
            log.info("   Cornerstone: filling %s", step)
            self._fill(page, profile, ctx)
            ctx["_csod_filled"] = step
            ctx["_csod_advances"] = 0
        else:
            attempts = ctx.get("_csod_advances", 0) + 1
            ctx["_csod_advances"] = attempts
            if attempts > _CSOD_MAX_ADVANCES:
                return f"failed: validation error — Cornerstone {step} would not advance"
            outcome = self._advance(page, allow_submit=not ctx.get("dry_run"))
            if outcome == "submit_blocked":
                log.info("   Cornerstone: dry run — stopping before Submit")
                return "dry_run"
            if not outcome:
                return "failed: no Next/Submit button found"

        # The generic filler guesses on this markup and corrupts the form, so
        # this handler is the only thing that touches it.
        ctx["skip_step"] = True
        return None

    def detect_success(self, page, ctx) -> bool:
        return self._is_confirmation(page)

    # ---------------------------------------------------------------- helpers

    @classmethod
    def _is_application_form(cls, page) -> bool:
        """True anywhere inside the CSOD application wizard.

        Step 2 carries none of step 1's markers -- it is screening questions
        and Submit -- so keying only on those made the handler stand down
        there, and the generic login-wall detector mistook the page for an
        account gate ("skipped: requires account").
        """
        try:
            if cls._step_key(page) != "application":
                return True
            return bool(
                page.evaluate(
                    "() => !!document.querySelector(\"[aria-labelledby^='actionItem.'],"
                    "#resumeFileUpload, [id^='EEOQuestion-']\")"
                )
            )
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _is_confirmation(page) -> bool:
        try:
            text = page.evaluate("document.body?.innerText?.toLowerCase() || ''")
            # Linde's confirmation reads "Thank You! You have successfully
            # applied to <role>" -- matching only "successfully submitted"
            # reported two genuinely submitted applications as failures.
            return any(
                p in text
                for p in (
                    "successfully applied",
                    "thank you for applying",
                    "application submitted",
                    "your application has been submitted",
                    "successfully submitted",
                    "application has been received",
                )
            )
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _step_key(page) -> str:
        try:
            found = page.evaluate(
                "() => (document.body.innerText.match(/Step\\s*\\d+\\s*of\\s*\\d+/i)||[])[0] || ''"
            )
        except Exception:  # noqa: BLE001
            found = ""
        return found or "application"

    def _fill(self, page, profile, ctx) -> None:
        for action in (
            self._upload_resume,
            self._fill_action_items,
            self._fill_aria_labelled,
            self._fill_country,
            self._fill_degree_selects,
            self._prune_incomplete_blocks,
            self._fill_eeo_selects,
            self._answer_radio_groups,
            self._fill_required_text_questions,
            self._answer_source_question,
        ):
            try:
                action(page, profile)
            except Exception as exc:  # noqa: BLE001
                log.debug("Cornerstone: %s failed: %s", action.__name__, str(exc)[:120])

    @staticmethod
    def _upload_resume(page, profile) -> None:
        if not getattr(profile, "resume_path", ""):
            return
        from pathlib import Path

        resume = Path(profile.resume_path).expanduser()
        if not resume.exists():
            return
        el = page.query_selector("#resumeFileUpload")
        if not el:
            return
        el.set_input_files(str(resume))
        # CSOD parses the file server-side and then injects the Experience and
        # Education rows. Wait for them: filling before they exist silently
        # skipped the required Degree selects, and the step never advanced.
        try:
            page.wait_for_selector("[id^='$resume-field']", timeout=45000)
            page.wait_for_timeout(3000)  # let the last rows settle
        except Exception:  # noqa: BLE001
            log.debug("Cornerstone: résumé parse produced no fields")
        log.info("   Cornerstone: uploaded resume")

    @staticmethod
    def _fill_action_items(page, profile) -> None:
        """Fill the fields whose only identity is aria-labelledby=actionItem.*."""
        first, _, last = (profile.full_name or "").partition(" ")
        values = {
            "firstName": first,
            "lastName": last or first,
            "email": profile.email or "",
        }
        for key, value in values.items():
            if not value:
                continue
            el = page.query_selector(f"input[aria-labelledby^='actionItem.{key}.']")
            if el and not (el.input_value() or "").strip():
                el.fill(value)
                log.info("   Cornerstone: %s -> %s", key, value)

    @staticmethod
    def _fill_aria_labelled(page, profile) -> None:
        wanted = {
            "address line 1": profile.street_address or "",
            "city": profile.city or "",
            "state": profile.state or "",
            "zip code": profile.zip_code or "",
            "phone": profile.phone or "",
        }
        for el in page.query_selector_all("input[aria-label]"):
            label = (el.get_attribute("aria-label") or "").strip().lower()
            value = wanted.get(label)
            if not value:
                continue
            try:
                if (el.input_value() or "").strip():
                    continue
                el.fill(value)
            except Exception:  # noqa: BLE001, S112
                continue

    @staticmethod
    def _fill_country(page, profile) -> None:
        el = page.query_selector("#contactDetails_country")
        if not el:
            return
        country = profile.country or "United States"
        for candidate in (country, "United States", "United States of America"):
            try:
                el.select_option(label=candidate)
                return
            except Exception:  # noqa: BLE001, S112
                continue

    @staticmethod
    def _degree_option(degree: str) -> str:
        """Map a free-text degree to CSOD's fixed Degree list."""
        d = (degree or "").lower()
        if "phd" in d or "ph.d" in d or "doctor" in d:
            return "doctoral"
        if "master" in d or d.startswith(("ms", "m.s", "meng", "mba")):
            return "master's"
        if "associate" in d:
            return "associate's"
        if "bachelor" in d or d.startswith(("bs", "b.s", "ba", "b.a", "beng")):
            return "bachelor's"
        return ""

    def _fill_degree_selects(self, page, profile) -> None:
        """Set the required Degree selects on résumé-parsed Education rows.

        CSOD parses the résumé into Education rows but leaves 'Degree *'
        unset, and it blocks the step. The value comes from the profile -- it
        is never invented.
        """
        wanted = self._degree_option(getattr(profile, "education_degree", "") or "")
        if not wanted:
            return
        for el in page.query_selector_all("select[id^='$resume-field']"):
            try:
                if (el.input_value() or "").strip():
                    continue
                options = [o.strip() for o in el.evaluate("e => [...e.options].map(o => o.text)")]
                match = next((o for o in options if o.lower() == wanted), "")
                if match:
                    el.select_option(label=match)
                    log.info("   Cornerstone: degree -> %s", match)
            except Exception:  # noqa: BLE001, S112
                continue

    @staticmethod
    def _prune_incomplete_blocks(page, profile) -> None:
        """Delete résumé-parsed rows that still have empty required fields.

        CSOD's parser invents part-filled Education/Experience rows (a school
        with no field of study, an entirely blank row). They are required, so
        the step will not advance, and the missing values are not in the
        profile -- fabricating an academic or employment record to satisfy a
        validator is not acceptable. Removing the parser's noise is: the résumé
        itself stays attached, so nothing real is lost.
        """
        for _ in range(_CSOD_MAX_PRUNES):
            button = page.evaluate_handle("""() => {
                const vis = e => e.offsetParent !== null;
                const bad = [...document.querySelectorAll("[id^='$resume-field']")].filter(
                  e => vis(e)
                    && (e.required || e.getAttribute('aria-required') === 'true')
                    && !(e.value || '').trim());
                if (!bad.length) return null;
                let n = bad[0];
                for (let i = 0; i < 8 && n; i++) {
                  const btn = [...n.querySelectorAll('button, a')].find(
                    b => /^Delete /i.test((b.innerText || '').trim()));
                  if (btn) return btn;
                  n = n.parentElement;
                }
                return null;
            }""").as_element()
            if not button:
                return
            caption = (button.inner_text() or "").strip()
            button.click()
            page.wait_for_timeout(1500)
            log.info("   Cornerstone: removed incomplete block (%s)", caption[:40])

    def _fill_eeo_selects(self, page, profile) -> None:
        """Answer the EEOQuestion-<n> selects from profile.self_identification."""
        self_id = getattr(profile, "self_identification", {}) or {}
        topics = (
            ("gender", ("gender",)),
            ("ethnicity", ("race", "ethnic")),
            ("veteran_status", ("veteran",)),
            ("disability", ("disability",)),
        )
        for el in page.query_selector_all("select[id^='EEOQuestion-']"):
            # aria-label is empty on these, so read the associated <label>.
            # Falling straight through to the container's innerText picked up
            # the OPTION list instead of the question, and matched the gender
            # select on the word "answer" -- declining a question the profile
            # actually answers.
            label = (
                el.evaluate(
                    "e => e.getAttribute('aria-label')"
                    " || (document.querySelector(`label[for='${e.id}']`)||{}).innerText"
                    " || ''"
                )
                or ""
            ).lower()
            options = [o.strip() for o in el.evaluate("e => [...e.options].map(o => o.text)")]

            wanted = ""
            for key, needles in topics:
                if any(n in label for n in needles):
                    wanted = str(self_id.get(key, "") or "")
                    break

            choice = self._match_option(wanted, options)
            if choice:
                el.select_option(label=choice)
                log.info("   Cornerstone: %s -> %s", label[:34], choice[:34])

    @staticmethod
    def _match_option(wanted: str, options: list) -> str:
        """Pick the option matching a profile answer, defaulting to declining."""
        real = [o for o in options if o.lower() not in ("", "please select")]
        if not real:
            return ""
        w = wanted.strip().lower()
        decline = next((o for o in real if _DECLINE_OPTION in o.lower()), "")

        if not w or any(d in w for d in _DECLINE_VALUES):
            return decline
        for opt in real:  # exact, then substring either direction
            if opt.lower() == w:
                return opt
        for opt in real:
            if w in opt.lower() or opt.lower() in w:
                return opt
        return decline

    @staticmethod
    def _answer_radio_groups(page, profile) -> None:
        """Answer every radio group on the page.

        Two kinds appear. Voluntary self-identification (Section 4212 military
        service) offers "I do not want to answer" and follows the candidate's
        stated preference. Screening questions on step 2 (noncompete, prior
        employment, work authorization) are real Yes/No questions and are
        resolved from the profile by the shared screening logic rather than
        being declined or guessed.
        """
        from jobapply.forms import _pick_radiogroup_option

        seen = set()
        for radio in page.query_selector_all("input[type='radio']"):
            name = radio.get_attribute("name") or ""
            if not name or name in seen:
                continue
            seen.add(name)
            group = page.query_selector_all(f"input[type='radio'][name='{name}']")
            opts = []
            for candidate in group:
                text = (
                    candidate.evaluate(
                        "e => (document.querySelector(`label[for='${e.id}']`)||{}).innerText"
                        " || e.getAttribute('aria-label') || e.value || ''"
                    )
                    or ""
                ).strip()
                opts.append(
                    {"id": candidate.get_attribute("id") or "", "text": text, "el": candidate}
                )

            question = (
                radio.evaluate(
                    "e => { let n = e.parentElement;"
                    " for (let i = 0; i < 7 && n; i++) {"
                    "   const t = (n.innerText || '').trim();"
                    "   if (t.length > 40) return t;"
                    "   n = n.parentElement; } return ''; }"
                )
                or ""
            ).strip()

            decline = next((o for o in opts if _DECLINE_OPTION in o["text"].lower()), None)
            voluntary = any(
                w in question.lower()
                for w in ("voluntary", "self-identif", "not required to", "completely voluntary")
            )
            yes_opt = next((o for o in opts if o["text"].strip().lower() == "yes"), None)
            consent = any(p in question.lower() for p in _CONSENT_PATTERNS)

            # Leave an existing selection alone -- except a consent question
            # sitting on its default. Linde ships the recruitment privacy
            # notice pre-set to "No" and then refuses Submit until it is
            # "Yes", and skipping any answered group meant never touching it.
            if any(r.is_checked() for r in group):
                if not (consent and yes_opt) or yes_opt["el"].is_checked():
                    continue
                log.info("   Cornerstone: consent defaulted to No — setting Yes")

            chosen = None
            if decline and voluntary:
                chosen = decline
            elif consent and yes_opt:
                chosen = yes_opt
            else:
                rid, _ = _pick_radiogroup_option(
                    question, [{"id": o["id"], "text": o["text"]} for o in opts], profile
                )
                chosen = next((o for o in opts if rid and o["id"] == rid), None) or decline

            if chosen and CornerstoneHandler._select_radio(chosen["el"]):
                log.info(
                    "   Cornerstone: %s -> %s",
                    question[:44].replace("\n", " "),
                    chosen["text"][:26],
                )

    @staticmethod
    def _question_for(el) -> str:
        """The question text wrapping a control (CSOD gives these no label).

        Climbs for a question-sized ancestor. Seven hops was too shallow for
        the step-2 questions -- it found nothing, and the caller skipped them
        in silence -- and a bare length test can also latch onto a whole
        section, so prefer a block that reads like a question (ends in * or
        contains ?) and cap how much text counts as one.
        """
        try:
            return (
                el.evaluate(
                    "e => { let n = e.parentElement, fallback = '';"
                    " for (let i = 0; i < 14 && n; i++) {"
                    "   const t = (n.innerText || '').trim();"
                    "   if (t.length >= 15 && t.length <= 400) {"
                    "     if (/[?*]/.test(t)) return t;"
                    "     if (!fallback) fallback = t; }"
                    "   n = n.parentElement; }"
                    " return fallback; }"
                )
                or ""
            ).strip()
        except Exception:  # noqa: BLE001
            return ""

    @classmethod
    def _fill_required_text_questions(cls, page, profile) -> None:
        """Answer required free-text questions (e.g. salary expectation).

        Values come from profile.screening_answers only. An unanswered
        question is left empty and surfaces as a validation failure rather
        than being invented -- a salary expectation the candidate never stated
        would misrepresent him on a binding application.
        """
        from jobapply.forms import _match_screening_answer

        # CSOD marks the WRAPPER aria-required, not the input, so filtering on
        # the control's own attribute matched nothing. Offer every empty text
        # box to the profile instead and fill only what it can answer.
        for el in page.query_selector_all("input[type='text'], input:not([type]), textarea"):
            try:
                if (el.input_value() or "").strip():
                    continue
                question = cls._question_for(el)
                if not question:
                    continue
                answer = _match_screening_answer(question, profile.screening_answers or {})
                if not answer:
                    log.warning(
                        "   Cornerstone: no profile answer for %r — leaving blank",
                        question[:70].replace("\n", " "),
                    )
                    continue
                el.scroll_into_view_if_needed(timeout=5000)
                el.fill(str(answer))
                log.info(
                    "   Cornerstone: %s -> %s", question[:44].replace("\n", " "), str(answer)[:26]
                )
            except Exception:  # noqa: BLE001, S112
                continue

    @classmethod
    def _answer_source_question(cls, page, profile) -> None:
        """Tick 'How did you hear about this job posting?'.

        Found top-down: the option labels are long enough that climbing up
        from a checkbox stops on a sibling option before ever reaching the
        question. These postings come from LinkedIn search, so that is the
        truthful answer; the list is checkboxes despite allowing one pick.
        """
        box = page.evaluate_handle("""() => {
            const vis = e => e.offsetParent !== null;
            // The innermost element matching the question is the LABEL alone;
            // the options live a level up. Require the block to hold the
            // checkboxes too, then take the tightest such block.
            const blocks = [...document.querySelectorAll('div, fieldset, section')].filter(
              e => vis(e)
                && /how did you hear/i.test(e.innerText || '')
                && e.querySelector("input[type=checkbox]"));
            if (!blocks.length) return null;
            const block = blocks[blocks.length - 1];
            return [...block.querySelectorAll("input[type=checkbox]")].find(c => {
              if (c.checked || !vis(c)) return false;
              const lab = (document.querySelector(`label[for='${c.id}']`) || {}).innerText
                       || (c.closest('label') || {}).innerText
                       || (c.closest('div') || {}).innerText || '';
              return /linkedin/i.test(lab);
            }) || null;
        }""").as_element()
        if not box:
            return
        try:
            box.scroll_into_view_if_needed(timeout=5000)
        except Exception:  # noqa: BLE001, S110
            pass
        if cls._select_radio(box):
            log.info("   Cornerstone: source -> LinkedIn")

    @staticmethod
    def _select_radio(el) -> bool:
        """Tick a radio, scrolling it into view first.

        The step-2 form is long enough that every screening radio sits below
        the fold, and check() refuses with "Element is outside of the
        viewport" -- so all seven answers were computed correctly and then
        silently dropped. Falls back to clicking the wrapping label and then to
        a scripted click, verifying the result each time.
        """
        try:
            el.scroll_into_view_if_needed(timeout=5000)
        except Exception:  # noqa: BLE001, S110
            pass

        attempts = (
            lambda: el.check(force=True),
            lambda: el.evaluate("e => (e.closest('label') || e).click()"),
            lambda: el.evaluate(
                "e => { e.checked = true; e.dispatchEvent(new Event('change', {bubbles: true})); }"
            ),
        )
        for attempt in attempts:
            try:
                attempt()
                if el.is_checked():
                    return True
            except Exception:  # noqa: BLE001, S112
                continue
        return False

    @staticmethod
    def _advance(page, allow_submit: bool = True) -> str:
        """Advance the wizard. Returns which control was used, or "".

        Never clicks the 'Add ...' links -- those append empty Professional
        Experience / Education / Skills blocks, inflating the form without
        progressing it. Returns "submit_blocked" when the only way forward is
        Submit and submitting is not permitted (dry run).
        """
        submit_texts = ("Submit", "Submit Application", "Apply")
        for text in ("Next", *submit_texts):
            if text in submit_texts and not allow_submit:
                continue
            for el in page.query_selector_all("button, input[type='submit'], a[role='button']"):
                try:
                    if not el.is_visible() or el.is_disabled():
                        continue
                    caption = (el.inner_text() or el.get_attribute("value") or "").strip()
                    if caption.lower() != text.lower():
                        continue
                    log.info("   Cornerstone: clicking '%s'", caption)
                    el.click()
                    page.wait_for_timeout(4000)
                    return "submit" if text in submit_texts else "next"
                except Exception:  # noqa: BLE001, S112
                    continue

        if not allow_submit and CornerstoneHandler._has_submit(page):
            return "submit_blocked"
        return ""

    @staticmethod
    def _has_submit(page) -> bool:
        try:
            return bool(
                page.evaluate("""() => [...document.querySelectorAll('button, input[type=submit]')]
                    .some(b => b.offsetParent !== null
                            && /^(submit|submit application|apply)$/i.test(
                                 (b.innerText || b.value || '').trim()))""")
            )
        except Exception:  # noqa: BLE001
            return False


register("Cornerstone", CornerstoneHandler)
