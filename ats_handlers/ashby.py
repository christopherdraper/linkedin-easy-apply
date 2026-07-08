"""Ashby ATS handler.

Ashby uses an application-level spam filter that blocks automated submissions.
This is NOT IP-based (proxy doesn't help). The filter may block:
- On submit (banner appears after clicking submit)
- On page load (banner appears immediately; we were flagged from prior attempts)

The handler checks both cases and returns a clean failure status, saving
CAPTCHA credits and avoiding wasted form-filling attempts.
"""

import logging
from typing import Optional

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register

log = logging.getLogger("job_apply")


class AshbyHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Ashby"

    def pre_flight(self, page, ctx: dict) -> Optional[str]:
        """Short-circuit embedded Ashby (``?ashby_jid=``) by navigating
        directly to the ``jobs.ashbyhq.com`` iframe URL, then check for a
        spam-banner rejection.

        Voleon, Vultr, and similar sites put the Ashby application form in
        an iframe on their careers page. The form-step loop never sees inputs
        on the outer page so it stalls at step 3. Mirroring the Greenhouse
        ``?gh_jid=`` iframe-jump fixes the same class of failure for Ashby.
        """
        if "ashby_jid=" in (page.url or "") and "ashbyhq.com" not in page.url:
            try:
                iframe = page.query_selector("iframe[src*='ashbyhq.com']")
                if iframe:
                    src = iframe.get_attribute("src") or ""
                    # Strip the embed=js suffix so we land on the full page,
                    # not the JS-only embed frame.
                    src = (
                        src.replace("&embed=js", "")
                        .replace("?embed=js&", "?")
                        .replace("?embed=js", "")
                    )
                    # Ashby's iframe lands on the job-detail page; rewrite to
                    # the /application suffix so the form-step loop sees inputs
                    # immediately rather than having to click 'Apply for this Job'.
                    if src and "/application" not in src:
                        # Insert /application before the query string
                        if "?" in src:
                            base, q = src.split("?", 1)
                            src = base.rstrip("/") + "/application?" + q
                        else:
                            src = src.rstrip("/") + "/application"
                    if src:
                        log.info("   Ashby: jumping to embedded application URL: %s", src[:100])
                        page.goto(src, timeout=30000)
                        page.wait_for_timeout(2000)
            except Exception as e:  # noqa: BLE001
                log.debug("Ashby embed iframe jump failed: %s", e)

        # Force the shared captcha detector to classify the captcha as
        # reCAPTCHA v3 (passive) by removing the v3 anchor iframe. With the
        # iframe gone and the .grecaptcha-badge still present, the detector's
        # ``isV3 = !!recapBadge && !recapFrame`` evaluates true, so the solve
        # uses 2captcha's v3 endpoint. v3 tokens carry browser-derived signals
        # (mouse, scroll, time-on-page) that Ashby's spam filter actually
        # reads; v2 tokens look like obvious bot solves and get rejected.
        # The grecaptcha JS library stays loaded (script tag is untouched) so
        # token verification still works at submit time.
        try:
            page.evaluate(
                "() => document.querySelectorAll('iframe[src*=\"recaptcha\"]')"
                ".forEach(e => e.remove())"
            )
        except Exception:  # noqa: BLE001, S110
            pass

        # Mild humanization to lift the reCAPTCHA v3 score: scroll and
        # mouse-jitter. reCAPTCHA v3 weighs mouse movement and scroll heavily.
        try:
            self._humanize(page)
        except Exception:  # noqa: BLE001, S110
            pass

        return self._check_spam_banner(page, "pre-flight")

    def on_step_start(self, page, ctx: dict) -> Optional[str]:
        """Detect spam rejection on each form-step iteration, plus fill any
        required react-datepicker fields the generic loop can't reach, plus
        nudge React state into matching the DOM values our generic fill set.

        Ashby is a React SPA; the pre_flight hook may run before the client
        hydrates and renders the spam banner. The form-step loop gives React
        multiple chances to finish rendering before we declare the form stuck.
        """
        # Fill react-datepicker date inputs (e.g. "When can you start a new
        # role?"). The generic field handler skips them because the input is
        # disabled until the calendar popup is opened, and AI answers the
        # label as text ("2 weeks") instead of a date.
        self._fill_required_date_pickers(page)

        # Sync React state to DOM values. Playwright's .fill() updates the
        # DOM value attribute and dispatches an "input" event, but Ashby's
        # form keeps the Submit button disabled until React's internal state
        # registers the change -- which requires going through the React
        # value setter directly. Re-fire events on every filled input so
        # React's onChange handler sees the canonical value.
        self._sync_react_state(page)

        # Check Ashby's "I acknowledge / I confirm" consent boxes (the
        # question text lives on the input's name attribute, which the
        # generic checkbox handler doesn't inspect). Submit stays disabled
        # until both are ticked.
        self._check_consent_checkboxes(page)

        # Fill Ashby's location combobox ("Where are you currently located?").
        # The generic location handler matches on "location" or "city", but
        # this label is "located" (past tense), so it misses. Combobox needs
        # a click-suggestion, not just typed text.
        profile = ctx.get("profile") if isinstance(ctx, dict) else None
        if profile:
            self._fill_location_combobox(page, profile)

        return self._check_spam_banner(page, "per-step")

    def on_submit_clicked(self, page, ctx: dict) -> Optional[str]:
        """Detect spam rejection after clicking submit."""
        return self._check_spam_banner(page, "post-submit")

    @staticmethod
    def _check_spam_banner(page, when: str) -> Optional[str]:
        try:
            body_text = page.evaluate(
                "() => (document.body ? document.body.innerText : '').toLowerCase()"
            )
            if "flagged as possible spam" in body_text or "flagged as spam" in body_text:
                log.warning("   Ashby: application flagged as spam (%s)", when)
                return "failed: Ashby spam filter"
        except Exception:  # noqa: BLE001
            pass
        return None

    @staticmethod
    def _fill_location_combobox(page, profile) -> None:
        """Fill Ashby location autocomplete combobox by typing + clicking
        first suggestion.

        Targets the "Where are you currently located?" question (and any
        similar Ashby combobox with placeholder "Start typing..."). Skips
        if already selected.
        """
        # No hardcoded fallback: without a profile city, skip rather than
        # type someone else's location into a live application.
        city = getattr(profile, "city", None)
        if not city:
            return
        try:
            comboboxes = page.query_selector_all(
                "input[role='combobox'][placeholder*='Start typing']"
            )
        except Exception:  # noqa: BLE001
            return
        for cb in comboboxes:
            try:
                if not cb.is_visible():
                    continue
                # Skip if a tag is already selected (Ashby renders selected
                # values as adjacent <span> "tags" or sets aria-expanded etc.)
                if (cb.input_value() or "").strip() and cb.evaluate(
                    "el => el.parentElement?.querySelector('[class*=tag]') !== null"
                ):
                    continue
                cb.click()
                page.wait_for_timeout(200)
                cb.fill("")
                cb.type(city, delay=60)
                page.wait_for_timeout(1800)
                # Find the first visible role=option in the listbox tied to
                # *this* combobox. Don't fall back to [class*='_option_']:
                # Ashby's Yes/No toggle buttons also carry _option_y2cw4_*
                # classes and they would mis-match.
                picked = page.evaluate("""() => {
                    const all = document.querySelectorAll("[role='option']");
                    for (const o of all) {
                        const r = o.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        const text = (o.innerText || '').trim();
                        if (!text) continue;
                        o.click();
                        return text.slice(0, 60);
                    }
                    return null;
                }""")
                if picked:
                    page.wait_for_timeout(300)
                    log.info("   Ashby: filled location combobox -> %s", picked)
                else:
                    # No suggestion appeared — clear so we don't leave stray text
                    cb.fill("")
                    log.debug("Ashby: no location suggestion for %r", city)
            except Exception as e:  # noqa: BLE001
                log.debug("Ashby location combobox fill failed: %s", e)

    @staticmethod
    def _check_consent_checkboxes(page) -> None:
        """Check Ashby acknowledgement checkboxes by their ``name`` attribute.

        Ashby renders acknowledgement checkboxes (e.g. "I acknowledge that I
        have opened the Arbitration Agreement") as ``<input type="checkbox"
        name="<full question text>">`` -- the question text IS the name.
        The generic checkbox handler reads sibling/parent text instead, and
        on the post-pre-flight DOM (after our captcha-iframe strip) it no
        longer reliably finds the consent phrase, so both consent boxes
        stay unchecked and Submit stays disabled.

        Also handles the cascade where checkbox B is initially ``disabled=""``
        until checkbox A is checked: re-scan once after first pass.
        """
        # Match on any consent verb (substring); avoids missing "I HEREBY
        # certify..." which the more specific "i certify" pattern won't match.
        # "certificate"/"certificat" would still false-positive but Ashby
        # uses these in consent boxes, not in document-upload labels.
        consent_phrases = (
            "acknowledge",
            "i agree",
            "i confirm",
            "certify",
            "i understand",
            "i consent",
            "i authorize",
        )
        for _attempt in range(2):
            try:
                cbs = page.query_selector_all("input[type='checkbox']")
            except Exception:  # noqa: BLE001
                return
            any_clicked = False
            for cb in cbs:
                try:
                    if cb.is_checked():
                        continue
                    name = (cb.get_attribute("name") or "").lower()
                    if not any(p in name for p in consent_phrases):
                        continue
                    if cb.get_attribute("disabled") is not None:
                        # Will become enabled after first consent box
                        continue
                    cb.check(force=True)
                    page.wait_for_timeout(200)
                    log.info("   Ashby: checked consent -> %s", name[:60])
                    any_clicked = True
                except Exception as e:  # noqa: BLE001
                    log.debug("Ashby consent click failed: %s", e)
            if not any_clicked:
                return

    @staticmethod
    def _sync_react_state(page) -> None:
        """Force React internal state to match DOM values for filled inputs.

        Playwright's .fill() sets the input's value attribute and fires an
        ``input`` event, but doesn't go through the prototype value setter
        that React's controlled-input pattern hooks. As a result, React's
        component state can stay empty even though the DOM looks correct,
        and Ashby's Submit button (gated on React state) stays disabled.

        Walking through all visible text/email/tel inputs and re-applying
        their value via the prototype setter, then dispatching input +
        change + blur, is the standard React-Testing-Library workaround.
        """
        try:
            page.evaluate("""() => {
                const sels = ["input[type='text']", "input[type='email']",
                              "input[type='tel']", "input[type='url']",
                              "textarea"];
                const seen = new Set();
                for (const sel of sels) {
                    for (const el of document.querySelectorAll(sel)) {
                        if (seen.has(el)) continue;
                        seen.add(el);
                        if (!el.value) continue;
                        try {
                            const proto = Object.getPrototypeOf(el);
                            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                            if (desc && desc.set) {
                                const v = el.value;
                                desc.set.call(el, '');
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                desc.set.call(el, v);
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                                el.dispatchEvent(new Event('blur', { bubbles: true }));
                            }
                        } catch (e) {}
                    }
                }
            }""")
        except Exception:  # noqa: BLE001, S110
            pass

    @staticmethod
    def _fill_required_date_pickers(page) -> None:
        """Fill any required react-datepicker inputs that are still empty.

        Ashby uses ``react-datepicker`` for "When can you start a new role?"
        and similar fields. The visible <input> is ``disabled=""`` until the
        calendar popup opens, so generic field-filling skips it. Click input
        to open popup, click a date roughly two weeks out, then close.

        Idempotent: skips already-filled pickers.
        """
        from datetime import datetime, timedelta

        try:
            wrappers = page.query_selector_all(".react-datepicker-wrapper")
        except Exception:  # noqa: BLE001
            return

        target_date = datetime.now() + timedelta(days=14)

        for wrapper in wrappers:
            try:
                inp = wrapper.query_selector("input[required]")
                if not inp:
                    continue
                # Skip if already has a value
                if (inp.get_attribute("value") or "").strip():
                    continue
                # Click to open popup; react-datepicker re-enables the input
                inp.click(force=True)
                page.wait_for_timeout(400)
                # If target month differs from displayed month, click the
                # forward arrow until matched (up to 4 hops).
                target_month_label = target_date.strftime("%B %Y")
                for _ in range(4):
                    visible_label = page.query_selector(
                        ".react-datepicker__current-month, .react-datepicker__header"
                    )
                    if visible_label and target_month_label in (visible_label.inner_text() or ""):
                        break
                    nxt = page.query_selector(".react-datepicker__navigation--next:visible")
                    if nxt:
                        nxt.click()
                        page.wait_for_timeout(250)
                    else:
                        break
                # Pick the target day. aria-label format varies by locale; try
                # both "Choose <weekday>, <month> <day>, <year>" and the
                # numbered class fallback.
                aria = target_date.strftime("Choose %A, %B %-d, %Y")
                day_btn = page.query_selector(f"[aria-label='{aria}']")
                if not day_btn:
                    day_cls = f".react-datepicker__day--{target_date.day:03d}"
                    day_btn = page.query_selector(
                        f"{day_cls}:not(.react-datepicker__day--outside-month)"
                        ":not(.react-datepicker__day--disabled)"
                    )
                if day_btn and day_btn.is_visible():
                    day_btn.click()
                    page.wait_for_timeout(300)
                    log.info(
                        "   Ashby: filled date picker -> %s",
                        target_date.strftime("%Y-%m-%d"),
                    )
                else:
                    log.debug("Ashby: target day %s not findable; skipping picker", aria)
            except Exception as e:  # noqa: BLE001
                log.debug("Ashby date-picker fill failed: %s", e)

    @staticmethod
    def _humanize(page) -> None:
        """Generate human-ish mouse/scroll signals for reCAPTCHA v3 scoring.

        Ashby + OpenAI's spam filter rejects submissions where the browser
        produced no mouse-movement or scroll events between page load and
        submit. This method synthesizes a few of each. Best-effort, swallows
        errors.
        """
        import random

        try:
            # Random mouse moves across the viewport
            for _ in range(6):
                x = random.randint(50, 800)  # noqa: S311
                y = random.randint(50, 600)  # noqa: S311
                page.mouse.move(x, y, steps=random.randint(5, 15))  # noqa: S311
                page.wait_for_timeout(random.randint(80, 250))  # noqa: S311
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            # Scroll down then back up
            page.mouse.wheel(0, 400)
            page.wait_for_timeout(random.randint(150, 400))  # noqa: S311
            page.mouse.wheel(0, 200)
            page.wait_for_timeout(random.randint(150, 400))  # noqa: S311
            page.mouse.wheel(0, -300)
            page.wait_for_timeout(random.randint(150, 400))  # noqa: S311
        except Exception:  # noqa: BLE001, S110
            pass


register("Ashby", AshbyHandler)
