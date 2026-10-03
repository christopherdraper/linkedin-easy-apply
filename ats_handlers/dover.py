"""Dover (app.dover.com/apply/...) handler.

Dover's form is MUI with no <label> elements: each field's caption is a sibling
<div class="...FormLabel..."> ("First Name *"), and inputs carry React ids like
":r2:". The generic label lookup therefore found nothing and every field was
skipped (2026-10-03, Kintsugi: "filled 0 fields"). This handler

- fills the fixed contact inputs by their stable name attributes,
- copies each FormLabel caption onto its input as aria-label, so the generic
  pipeline can answer any custom questions the employer added,
- uploads the PDF resume into the "Resume" input (Dover accepts only .pdf) and
  keeps the generic uploader away from the "Autofill from resume" input, which
  would overwrite the filled fields with its own parse,
- waits for the Cloudflare Turnstile widget drawn under the filled form. Its
  checkbox ignores automated clicks, so the generic captcha step solves it
  through the captcha service.
"""

import logging
from pathlib import Path
from typing import Optional

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register

log = logging.getLogger(__name__)

# Caption of the nearest field: walk up from the input until an ancestor's
# previous sibling is a Dover FormLabel. Stops at the form.
_LABEL_DOVER_FIELDS_JS = """() => {
  const caption = (el) => {
    for (let n = el; n && n.tagName !== 'FORM'; n = n.parentElement) {
      const prev = n.previousElementSibling;
      if (prev && /FormLabel/.test(prev.className || '')) return prev.innerText.trim();
    }
    return '';
  };
  let n = 0;
  for (const el of document.querySelectorAll('form input, form textarea, form select')) {
    if (el.type === 'file' || el.type === 'hidden' || el.getAttribute('aria-label')) continue;
    const text = caption(el);
    if (text) { el.setAttribute('aria-label', text); n++; }
  }
  return n;
}"""

# The resume input's block is captioned "Resume"; the autofill block is not
# captioned and says "Autofill from resume".
_FILE_INPUT_ROLES_JS = """() => [...document.querySelectorAll('form input[type=file]')].map((el, i) => {
  let text = '';
  for (let n = el; n && n.tagName !== 'FORM' && !text; n = n.parentElement) {
    const prev = n.previousElementSibling;
    if (prev && /FormLabel/.test(prev.className || '')) text = prev.innerText;
  }
  const block = (el.closest('[role=button]') || el.parentElement || el).innerText || '';
  return {i, caption: text.trim(), autofill: /autofill/i.test(block) && !text};
})"""


class DoverHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Dover"

    def on_step_start(self, page, ctx: dict) -> Optional[str]:
        profile = ctx.get("profile") if isinstance(ctx, dict) else None
        try:
            has_form = page.query_selector("form input[name='firstName']") is not None
        except Exception:  # noqa: BLE001
            has_form = False
        if not (profile and has_form):
            return None
        filled = self._fill_contact_fields(page, profile)
        filled += self._upload_resume(page, profile)
        try:
            labelled = page.evaluate(_LABEL_DOVER_FIELDS_JS)
        except Exception as e:  # noqa: BLE001
            labelled = 0
            log.debug("Dover: labelling fields failed: %s", e)
        # The Turnstile widget is drawn only once the form is filled in; wait
        # for it so the generic captcha step, which runs next, can solve it.
        if filled:
            # Its container exists at once but has zero height until drawn.
            try:
                page.wait_for_function(
                    """() => {
                        const i = document.querySelector("input[name='cf-turnstile-response']");
                        return !!i && i.parentElement.getBoundingClientRect().height > 0;
                    }""",
                    timeout=10000,
                )
            except Exception:  # noqa: BLE001, S110
                pass
        if filled or labelled:
            log.info(
                "   Dover: filled %d fields, labelled %d for the generic filler", filled, labelled
            )
        return None

    def detect_success(self, page, ctx: dict) -> bool:
        """Dover swaps the form for a "Thanks for applying!" card on the same URL.

        The generic classifier read that card as an error page (2026-10-03).
        """
        try:
            body = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception:  # noqa: BLE001
            return False
        return "thanks for applying" in body.lower() and not page.query_selector(
            "form input[name='firstName']"
        )

    @staticmethod
    def _fill_contact_fields(page, profile) -> int:
        first, _, last = (profile.full_name or "").partition(" ")
        values = {
            "firstName": first,
            "lastName": last,
            "email": profile.email,
            "linkedinUrl": profile.linkedin_url,
            "phoneNumber": profile.phone,
        }
        filled = 0
        for name, value in values.items():
            if not value:
                continue
            try:
                el = page.query_selector(f"form input[name='{name}']")
                if el and el.is_visible() and not (el.input_value() or "").strip():
                    el.fill(str(value))
                    filled += 1
            except Exception as e:  # noqa: BLE001
                log.debug("Dover: filling %s failed: %s", name, e)
        return filled

    @staticmethod
    def _upload_resume(page, profile) -> int:
        """Put the PDF resume in the Resume input; fence off the autofill input."""
        try:
            inputs = page.query_selector_all("form input[type=file]")
            roles = page.evaluate(_FILE_INPUT_ROLES_JS)
        except Exception as e:  # noqa: BLE001
            log.debug("Dover: file input scan failed: %s", e)
            return 0
        uploaded = 0
        pdf = Path(profile.resume_path or "").expanduser().with_suffix(".pdf")
        for role in roles:
            el = inputs[role["i"]]
            if role["autofill"]:
                el.evaluate("el => el.setAttribute('data-jobapply-skip', 'dover-autofill')")
                continue
            if "resume" not in role["caption"].lower() or el.get_attribute("data-jobapply-skip"):
                continue
            if not pdf.exists():
                log.warning("   Dover: needs a PDF resume, none at %s", pdf)
                continue
            el.set_input_files(str(pdf))
            el.evaluate("el => el.setAttribute('data-jobapply-skip', 'dover-resume')")
            log.info("   Dover: uploaded resume %s", pdf.name)
            uploaded += 1
        return uploaded


register("Dover", DoverHandler)
