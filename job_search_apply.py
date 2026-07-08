#!/usr/bin/env python3
"""
LinkedIn job search and auto-apply.
Supports Easy Apply (in-modal) and external ATS applications (Workday, Greenhouse, etc.).
Uses Claude AI for job scoring, cover letters, screening questions, and application notes.
"""

import argparse
import hashlib
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from ats_handlers import get_handler
from jobapply import stats
from jobapply.accounts import (
    _attempt_account_creation,
    _attempt_ats_login,
    _extract_code_from_email_body,
    _fetch_verification_code_from_gmail,
    _fill_registration_form,
    _generate_ats_password,
    _get_domain,
    _handle_registration_verification,
    _load_ats_accounts,
    _save_ats_account,
)
from jobapply.ai import _AI_AVAILABLE, _get_ai_client
from jobapply.applog import (
    already_applied,
    load_log,
    save_log,
    save_search_log,
)
from jobapply.browser import (
    _STEALTH,
    _USER_AGENTS,
    _dismiss_linkedin_overlays,
    _ensure_logged_in,
    _human_delay,
    _inject_session_cookies_if_needed,
    _load_credentials,
    _login_linkedin,
    _playwright_context,
    _resolve_proxy,
    _save_credentials,
    _save_session,
    _stealth_playwright,
)
from jobapply.config import (
    ATS_ACCOUNTS_FILE,
    CDP_URL,
    COVER_LETTER_DIR,
    CREDENTIALS_FILE,
    DATA_DIR,
    DEBUG_DIR,
    DEEP_APPLY_QUEUE_FILE,
    LOG_FILE,
    SEARCH_LOG_FILE,
    SESSION_FILE,
)
from jobapply.content import (
    _basic_cover_letter,
    _basic_notes,
    _ensure_cover_letter_docx,
    _save_cover_letter_docx,
    ai_build_notes,
    ai_generate_cover_letter,
    ai_score_job,
    score_job,
)
from jobapply.easy_apply import (
    _dismiss_save_dialog,
    _get_modal_text,
    _navigate_form,
    submit_easy_apply,
)
from jobapply.forms import (
    _FORM_FILL_SYSTEM,
    _FORM_FILL_TEXTAREA_SYSTEM,
    _ai_answer_question,
    _answer_radio_buttons,
    _answer_screening_questions,
    _answer_select_dropdowns,
    _answer_textareas,
    _best_option_match,
    _build_form_prompt,
    _check_mandatory_checkboxes,
    _clamp_to_maxlength,
    _contact_value_for_label,
    _determine_radio_answer,
    _dismiss_all_typeaheads,
    _dismiss_typeahead,
    _dump_form_debug,
    _fill_empty_required_fields,
    _fill_input_field,
    _get_field_label,
    _get_form_container,
    _get_validation_errors,
    _match_screening_answer,
    _safe_click,
)
from jobapply.outreach import (
    _ai_draft_hiring_message,
    _extract_hiring_manager,
    _message_hiring_manager_after_apply,
    _send_hiring_manager_message,
)
from jobapply.pages import (
    _GUEST_SELECTORS,
    _PAGE_CLASSIFIER_SYSTEM,
    _RESUME_UPLOAD_BYPASS_SELECTORS,
    _2captcha_solve,
    _capsolver_solve,
    _classify_page,
    _detect_captcha,
    _detect_login_page,
    _detect_success_or_confirmation,
    _extract_page_snapshot,
    _inject_captcha_token,
    _resolve_login_wall,
    _solve_captcha,
    _try_guest_bypass,
    _try_resume_upload_bypass,
    _wait_and_dismiss_cookies,
)
from jobapply.profile import (
    _STATE_NAMES,
    ApplicantProfile,
    JobSearchParams,
    _format_previous_employers,
    _profile_summary,
)
from jobapply.queue import (
    _DEEP_APPLY_ELIGIBLE_CATEGORIES,
    _deep_apply_eligible,
    _escalate_to_q3,
    _generate_deep_apply_prompt,
    _load_deep_apply_queue,
    _mark_deep_apply_done,
    _queue_for_deep_apply,
    _save_deep_apply_queue,
)
from jobapply.safety import (
    _ABORT_FIELD_PATTERNS,
    _INJECTION_PATTERNS,
    ApplicationAbortError,
    _check_field_label,
    _looks_like_injection,
    _sanitize_description,
)
from jobapply.search import (
    _BIOTECH_WORKDAY_SITES,
    _extract_results_count,
    _fetch_description,
    _parse_job_cards,
    _workday_job_detail,
    _workday_search,
    market_snapshot,
    search_biotech,
    search_hn_whos_hiring,
    search_linkedin,
    search_remoteok,
)
from jobapply.stats import (
    _ATS_PATTERNS,
    _categorize_failure,
    _compute_cost_usd,
    _detect_ats_platform,
)

# Facade re-exports: external consumers (ats_handlers/, assisted_apply_mcp.py,
# dashboard.py, tests/) import these names from job_search_apply. Listing them
# in __all__ marks them as intentionally exported for vulture and readers.
__all__ = [
    "ApplicantProfile",
    "ApplicationAbortError",
    "JobSearchParams",
    "ATS_ACCOUNTS_FILE",
    "CDP_URL",
    "COVER_LETTER_DIR",
    "CREDENTIALS_FILE",
    "DATA_DIR",
    "DEBUG_DIR",
    "DEEP_APPLY_QUEUE_FILE",
    "LOG_FILE",
    "SEARCH_LOG_FILE",
    "SESSION_FILE",
    "_ABORT_FIELD_PATTERNS",
    "_AI_AVAILABLE",
    "_ATS_PATTERNS",
    "_BIOTECH_WORKDAY_SITES",
    "_DEEP_APPLY_ELIGIBLE_CATEGORIES",
    "_FORM_FILL_SYSTEM",
    "_FORM_FILL_TEXTAREA_SYSTEM",
    "_GUEST_SELECTORS",
    "_INJECTION_PATTERNS",
    "_PAGE_CLASSIFIER_SYSTEM",
    "_RESUME_UPLOAD_BYPASS_SELECTORS",
    "_STATE_NAMES",
    "_STEALTH",
    "_USER_AGENTS",
    "_2captcha_solve",
    "_ai_answer_question",
    "_ai_draft_hiring_message",
    "_answer_radio_buttons",
    "_answer_screening_questions",
    "_answer_select_dropdowns",
    "_answer_textareas",
    "_attempt_account_creation",
    "_attempt_ats_login",
    "_basic_cover_letter",
    "_basic_notes",
    "_best_option_match",
    "_build_form_prompt",
    "_capsolver_solve",
    "_categorize_failure",
    "_check_field_label",
    "_check_mandatory_checkboxes",
    "_clamp_to_maxlength",
    "_classify_page",
    "_compute_cost_usd",
    "_contact_value_for_label",
    "_deep_apply_eligible",
    "_detect_ats_platform",
    "_detect_captcha",
    "_detect_login_page",
    "_detect_success_or_confirmation",
    "_determine_radio_answer",
    "_dismiss_all_typeaheads",
    "_dismiss_linkedin_overlays",
    "_dismiss_save_dialog",
    "_dismiss_typeahead",
    "_dump_form_debug",
    "_ensure_cover_letter_docx",
    "_ensure_logged_in",
    "_escalate_to_q3",
    "_extract_code_from_email_body",
    "_extract_hiring_manager",
    "_extract_page_snapshot",
    "_extract_results_count",
    "_fetch_description",
    "_fetch_verification_code_from_gmail",
    "_fill_empty_required_fields",
    "_fill_input_field",
    "_fill_registration_form",
    "_format_previous_employers",
    "_generate_ats_password",
    "_generate_deep_apply_prompt",
    "_get_ai_client",
    "_get_domain",
    "_get_field_label",
    "_get_form_container",
    "_get_modal_text",
    "_get_validation_errors",
    "_handle_registration_verification",
    "_human_delay",
    "_inject_captcha_token",
    "_inject_session_cookies_if_needed",
    "_load_ats_accounts",
    "_load_credentials",
    "_load_deep_apply_queue",
    "_login_linkedin",
    "_looks_like_injection",
    "_mark_deep_apply_done",
    "_match_screening_answer",
    "_message_hiring_manager_after_apply",
    "_navigate_form",
    "_parse_job_cards",
    "_playwright_context",
    "_profile_summary",
    "_queue_for_deep_apply",
    "_resolve_login_wall",
    "_resolve_proxy",
    "_safe_click",
    "_sanitize_description",
    "_save_ats_account",
    "_save_cover_letter_docx",
    "_save_credentials",
    "_save_deep_apply_queue",
    "_save_session",
    "_send_hiring_manager_message",
    "_solve_captcha",
    "_stealth_playwright",
    "_try_guest_bypass",
    "_try_resume_upload_bypass",
    "_wait_and_dismiss_cookies",
    "_workday_job_detail",
    "_workday_search",
    "ai_build_notes",
    "ai_generate_cover_letter",
    "ai_score_job",
    "already_applied",
    "load_log",
    "market_snapshot",
    "save_log",
    "save_search_log",
    "score_job",
    "search_biotech",
    "search_hn_whos_hiring",
    "search_linkedin",
    "search_remoteok",
    "submit_easy_apply",
]

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# External ATS form filler (Workday, Greenhouse, Lever, iCIMS, etc.)
# ---------------------------------------------------------------------------


_MAX_EXTERNAL_STEPS = 20


def _find_file_upload_inputs(page) -> list:
    """Find all file upload inputs on the page with their labels and IDs."""
    uploads = []
    try:
        for inp in page.query_selector_all("input[type='file']"):
            label = _get_field_label(page, inp)
            accept = inp.get_attribute("accept") or ""
            el_id = (inp.get_attribute("id") or "").lower()
            el_name = (inp.get_attribute("name") or "").lower()
            uploads.append(
                {
                    "element": inp,
                    "label": label,
                    "accept": accept,
                    "id": el_id,
                    "name": el_name,
                }
            )
    except Exception as exc:
        log.debug("File upload scan failed: %s", exc)
    return uploads


def _handle_file_uploads(
    page,
    profile: ApplicantProfile,
    cover_letter_path: str = "",
    uploaded_files: Optional[set] = None,
) -> int:
    """Upload resume and cover letter to file inputs on the page. Returns count uploaded.

    ``uploaded_files`` tracks element ids/names that have already been uploaded
    across form steps so we don't re-upload and corrupt the ATS upload state.
    """
    uploads = _find_file_upload_inputs(page)
    if not uploads:
        return 0

    if uploaded_files is None:
        uploaded_files = set()

    filled = 0
    resume_path = str(Path(profile.resume_path).expanduser()) if profile.resume_path else ""
    cover_letter_path = _ensure_cover_letter_docx(cover_letter_path) if cover_letter_path else ""

    resume_uploaded = "resume" in uploaded_files
    cover_uploaded = "cover" in uploaded_files

    for upload in uploads:
        label = upload["label"]
        el_id = upload.get("id", "")
        el_name = upload.get("name", "")
        element = upload["element"]
        # Combine label, id, and name for matching
        hints = f"{label} {el_id} {el_name}".lower()

        # Skip if this element was already uploaded in a previous step
        element_key = el_id or el_name or "file_input"
        if element_key in uploaded_files:
            continue

        try:
            if any(kw in hints for kw in ("resume", "cv", "curriculum")):
                if not resume_uploaded and resume_path and Path(resume_path).exists():
                    element.set_input_files(resume_path)
                    log.info(f"   📄 Uploaded resume: {Path(resume_path).name}")
                    resume_uploaded = True
                    uploaded_files.add("resume")
                    uploaded_files.add(element_key)
                    filled += 1
            elif any(kw in hints for kw in ("cover_letter", "cover letter", "coverletter")):
                if not cover_uploaded and cover_letter_path and Path(cover_letter_path).exists():
                    element.set_input_files(cover_letter_path)
                    log.info(f"   📄 Uploaded cover letter: {Path(cover_letter_path).name}")
                    cover_uploaded = True
                    uploaded_files.add("cover")
                    uploaded_files.add(element_key)
                    filled += 1
            else:
                # Unknown file field — upload resume if not yet uploaded, else cover letter
                if not resume_uploaded and resume_path and Path(resume_path).exists():
                    element.set_input_files(resume_path)
                    log.info(f"   📄 Uploaded resume (inferred) for '{label[:40]}'")
                    resume_uploaded = True
                    uploaded_files.add("resume")
                    uploaded_files.add(element_key)
                    filled += 1
                elif not cover_uploaded and cover_letter_path and Path(cover_letter_path).exists():
                    element.set_input_files(cover_letter_path)
                    log.info(f"   📄 Uploaded cover letter (inferred) for '{label[:40]}'")
                    cover_uploaded = True
                    uploaded_files.add("cover")
                    uploaded_files.add(element_key)
                    filled += 1
        except Exception as exc:
            log.debug("File upload failed for '%s': %s", label, exc)

    return filled


def _get_custom_dropdown_options(page, element) -> List[str]:  # noqa: C901
    """Get options from a custom (non-native) dropdown component."""
    try:
        # Click to expand the dropdown
        _safe_click(element, page)
        page.wait_for_timeout(400)

        options = []

        # Strategy 1: aria-owns / aria-controls → scoped listbox
        listbox_id = element.get_attribute("aria-owns") or element.get_attribute("aria-controls")
        if listbox_id:
            options = page.query_selector_all(f"#{listbox_id} [role='option']")

        # Strategy 2: React-Select pattern — derive listbox ID from element ID
        if not options:
            el_id = element.get_attribute("id") or ""
            if el_id:
                rs_listbox_id = f"react-select-{el_id}-listbox"
                options = page.query_selector_all(f"#{rs_listbox_id} [role='option']")

        # Strategy 3: aria-activedescendant → find parent listbox
        if not options:
            active_desc = element.get_attribute("aria-activedescendant") or ""
            if active_desc:
                active_el = page.query_selector(f"#{active_desc}")
                if active_el:
                    options = page.evaluate(
                        """(el) => {
                        const listbox = el.closest('[role="listbox"]');
                        if (!listbox) return [];
                        return [...listbox.querySelectorAll('[role="option"]')].map(o => o.id || '');
                    }""",
                        active_el,
                    )
                    if options and isinstance(options[0], str):
                        # Got IDs — resolve to elements
                        options = [page.query_selector(f"#{oid}") for oid in options if oid]
                        options = [o for o in options if o]

        # Strategy 4: broad fallback — only listboxes NOT from phone-country widgets
        if not options:
            options = page.evaluate("""() => {
                const results = [];
                for (const lb of document.querySelectorAll('[role="listbox"]')) {
                    if (lb.id && lb.id.includes('country-listbox')) continue;
                    if (lb.classList.contains('iti__country-list')) continue;
                    for (const opt of lb.querySelectorAll('[role="option"]')) {
                        results.push(opt.innerText.trim());
                    }
                }
                return results;
            }""")
            if options:
                # These are text strings already
                texts = [t for t in options[:20] if t]
                if not texts:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(200)
                return texts

        if not options:
            options = page.query_selector_all(
                "[class*='menu'] [role='option'], [class*='dropdown'] li, [class*='menu'] li"
            )

        texts = []
        for opt in options[:20]:
            text = opt.inner_text().strip()
            if text:
                texts.append(text)
        # Always close the dropdown after extracting options so that
        # _fill_custom_dropdown's re-open click actually opens it.
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        return texts
    except Exception as exc:
        log.debug("Custom dropdown option extraction failed: %s", exc)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return []


def _fill_custom_dropdown(
    page, element, label_text: str, options: List[str], profile: ApplicantProfile
) -> bool:
    """Fill a custom dropdown by asking AI to pick from the options."""
    if not options:
        return False

    options_str = ", ".join(options[:20])
    answer = _ai_answer_question(f"{label_text} (choose one: {options_str})", profile)
    if not answer:
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        return False

    # Re-open the dropdown (may have closed after option extraction)
    _safe_click(element, page)
    page.wait_for_timeout(400)

    # Find and click the matching option — scoped to this dropdown's listbox
    try:
        opt_elements = []
        el_id = element.get_attribute("id") or ""
        listbox_id = (
            element.get_attribute("aria-owns") or element.get_attribute("aria-controls") or ""
        )

        # Try scoped selectors first
        if listbox_id:
            opt_elements = page.query_selector_all(f"#{listbox_id} [role='option']")
        if not opt_elements and el_id:
            rs_listbox_id = f"react-select-{el_id}-listbox"
            opt_elements = page.query_selector_all(f"#{rs_listbox_id} [role='option']")
        if not opt_elements:
            # Fallback: all options excluding phone-country widgets
            opt_elements = page.query_selector_all(
                "[role='listbox']:not(.iti__country-list) [role='option']"
            )

        opt_texts_list = [el.inner_text().strip() for el in opt_elements]
        best_idx = _best_option_match(answer, opt_texts_list)
        if best_idx >= 0:
            _safe_click(opt_elements[best_idx], page)
            log.info(f"   🤖 Custom dropdown '{label_text[:40]}' → '{opt_texts_list[best_idx]}'")
            return True

        # No match found — close dropdown
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
    except Exception as exc:
        log.debug("Custom dropdown selection failed: %s", exc)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

    return False


def _answer_external_screening_questions(  # noqa: C901
    page,
    profile: ApplicantProfile,
    job_title: Optional[str] = None,
    company: Optional[str] = None,
) -> int:
    """
    Fill all form fields on an external ATS page using generic selectors.
    Reuses the existing field-filling pipeline. Returns count of fields filled.
    """
    filled = 0

    # Radio buttons — reuse existing handler (selectors are generic enough)
    try:
        _answer_radio_buttons(page, profile)
    except Exception as exc:
        log.debug("External radio buttons failed: %s", exc)

    # Ashby-style Yes/No button groups — these use <button> elements inside a
    # container with class containing "yesno", paired with a label via hidden checkbox
    try:
        yesno_containers = page.query_selector_all(
            "[class*='yesno'], [class*='yes-no'], [class*='YesNo']"
        )
        for container in yesno_containers:
            try:
                buttons = container.query_selector_all("button")
                if len(buttons) < 2:
                    continue
                btn_texts = [b.inner_text().strip() for b in buttons]
                # Check if already selected (one button will have an "active"/"selected" state)
                already_selected = container.evaluate("""el => {
                    const btns = el.querySelectorAll('button');
                    for (const b of btns) {
                        if (b.classList.toString().includes('selected')
                            || b.classList.toString().includes('active')
                            || b.getAttribute('aria-pressed') === 'true'
                            || b.getAttribute('data-selected') === 'true') return true;
                    }
                    return false;
                }""")
                if already_selected:
                    continue
                # Find the label (walk up to find the field entry container)
                label = container.evaluate("""el => {
                    let parent = el.parentElement;
                    for (let i = 0; i < 5 && parent; i++) {
                        const lbl = parent.querySelector('label');
                        if (lbl) return lbl.textContent.trim();
                        parent = parent.parentElement;
                    }
                    return '';
                }""")
                if not label:
                    continue
                _check_field_label(label)
                answer = _ai_answer_question(
                    f"{label} (choose one: {', '.join(btn_texts)})", profile
                )
                if answer:
                    for btn, btn_text in zip(buttons, btn_texts, strict=False):
                        if answer.lower() in btn_text.lower() or btn_text.lower() in answer.lower():
                            _safe_click(btn, page)
                            log.info(f"   🤖 Yes/No '{label[:40]}' → '{btn_text}'")
                            filled += 1
                            stats._field_fills.append(
                                {"field": label, "value": btn_text, "source": "ai_yesno"}
                            )
                            break
            except ApplicationAbortError:
                raise
            except Exception as exc:
                log.debug("Ashby Yes/No button failed: %s", exc)
    except Exception as exc:
        log.debug("Ashby Yes/No scan failed: %s", exc)

    # Generic button-group radio alternatives (Yes/No, True/False)
    # Catches ATS systems that use button elements instead of radio inputs
    try:
        for fieldset in page.query_selector_all(
            "[role='radiogroup'], [class*='radio-group'], [class*='option-group']"
        ):
            try:
                buttons = fieldset.query_selector_all("button, [role='radio']")
                if len(buttons) < 2:
                    continue
                # Check if already selected
                has_selection = any(
                    b.get_attribute("aria-checked") == "true"
                    or b.get_attribute("aria-pressed") == "true"
                    or "selected" in (b.get_attribute("class") or "")
                    or "active" in (b.get_attribute("class") or "")
                    for b in buttons
                )
                if has_selection:
                    continue
                btn_texts = [b.inner_text().strip() for b in buttons if b.inner_text().strip()]
                if not btn_texts:
                    continue
                label = _get_field_label(page, fieldset)
                if not label:
                    continue
                _check_field_label(label)
                answer = _ai_answer_question(
                    f"{label} (choose one: {', '.join(btn_texts)})", profile
                )
                if answer:
                    for btn in buttons:
                        bt = btn.inner_text().strip()
                        if answer.lower() in bt.lower() or bt.lower() in answer.lower():
                            _safe_click(btn, page)
                            log.info(f"   🤖 Button radio '{label[:40]}' → '{bt}'")
                            filled += 1
                            stats._field_fills.append(
                                {"field": label, "value": bt, "source": "ai_button_radio"}
                            )
                            break
            except ApplicationAbortError:
                raise
            except Exception as exc:
                log.debug("Button-group radio failed: %s", exc)
    except Exception as exc:
        log.debug("Button-group radio scan failed: %s", exc)

    # Native select dropdowns — reuse existing handler
    try:
        _answer_select_dropdowns(page, profile)
    except Exception as exc:
        log.debug("External select dropdowns failed: %s", exc)

    # Textareas — fill with AI (the built-in handler only matches exact screening_answers)
    try:
        for textarea in page.query_selector_all("textarea"):
            try:
                if textarea.input_value():
                    continue
                if not textarea.is_visible():
                    continue
            except Exception:
                continue
            label_text = _get_field_label(page, textarea)
            if not label_text:
                continue
            _check_field_label(label_text)
            answer = _ai_answer_question(
                label_text,
                profile,
                field_type="textarea",
                job_title=job_title,
                company=company,
            )
            if answer:
                textarea.fill(answer)
                filled += 1
                stats._field_fills.append(
                    {"field": label_text, "value": answer[:200], "source": "ai_textarea"}
                )
    except ApplicationAbortError:
        raise
    except Exception as exc:
        log.debug("External textareas failed: %s", exc)

    # Checkboxes — reuse existing handler
    try:
        _check_mandatory_checkboxes(page)
    except Exception as exc:
        log.debug("External checkboxes failed: %s", exc)

    # ExtJS boxselect widgets (GR8People and similar ATS platforms)
    # These render as <input class="x-form-field"> inside a .x-boxselect wrapper.
    # Standard .fill() doesn't trigger the ExtJS selection — must type + click option.
    try:
        for bs_input in page.query_selector_all(".x-boxselect input.x-form-field"):
            try:
                if not bs_input.is_visible():
                    continue
                # Skip if already has a selected tag (x-tagfield-item / x-boxselect-item)
                has_tag = bs_input.evaluate(
                    "el => !!el.closest('.x-boxselect')?.querySelector('.x-tagfield-item, "
                    "li.x-boxselect-item')"
                )
                if has_tag:
                    continue
                # Check if parent has "has-value" class — ExtJS sets this on filled selects
                has_value_cls = bs_input.evaluate(
                    "el => el.closest('.x-boxselect')?.className?.includes('has-valu') || false"
                )
                if has_value_cls:
                    continue
                # Skip phone country-code widgets
                aria_lbl = bs_input.get_attribute("aria-label") or ""
                if "country code" in aria_lbl.lower():
                    continue
            except Exception:
                continue
            label = _get_field_label(page, bs_input)
            if not label:
                continue
            _check_field_label(label)
            # Type a prefix to trigger the filter dropdown
            answer = _ai_answer_question(label, profile)
            if not answer:
                continue
            try:
                bs_input.click()
                bs_input.evaluate("el => el.value = ''")
                bs_input.type(answer[:20], delay=50)
                page.wait_for_timeout(1000)
                # Find the visible boundlist item
                option = page.query_selector(
                    ".x-boundlist-item:visible, .x-boundlist .x-boundlist-item"
                )
                if option and option.is_visible():
                    opt_text = option.inner_text().strip()
                    _safe_click(option, page)
                    page.wait_for_timeout(300)
                    log.info("   📋 ExtJS select '%s' → '%s'", label[:40], opt_text[:40])
                    filled += 1
                    stats._field_fills.append(
                        {"field": label, "value": opt_text, "source": "extjs_boxselect"}
                    )
                else:
                    # No match — clear and try shorter prefix
                    bs_input.evaluate("el => el.value = ''")
                    bs_input.type(answer[:5], delay=50)
                    page.wait_for_timeout(1000)
                    option = page.query_selector(
                        ".x-boundlist-item:visible, .x-boundlist .x-boundlist-item"
                    )
                    if option and option.is_visible():
                        opt_text = option.inner_text().strip()
                        _safe_click(option, page)
                        page.wait_for_timeout(300)
                        log.info("   📋 ExtJS select '%s' → '%s'", label[:40], opt_text[:40])
                        filled += 1
                        stats._field_fills.append(
                            {"field": label, "value": opt_text, "source": "extjs_boxselect"}
                        )
                    else:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(200)
            except ApplicationAbortError:
                raise
            except Exception as exc:
                log.debug("ExtJS boxselect failed for '%s': %s", label[:40], exc)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
    except Exception as exc:
        log.debug("ExtJS boxselect scan failed: %s", exc)

    # Custom dropdowns (non-native, ARIA-based)
    # Skip phone country-code widgets (intl-tel-input); handle location autocompletes specially
    _SKIP_COMBOBOX_IDS = {"iti-0__search-input", "iti-1__search-input"}
    _SKIP_COMBOBOX_CLASSES = {"iti__search-input"}
    _LOCATION_KEYWORDS = ("location", "city", "candidate-location")
    try:
        for combo in page.query_selector_all("[role='combobox']"):
            combo_id = combo.get_attribute("id") or ""
            combo_cls = combo.get_attribute("class") or ""
            # Skip intl-tel-input phone widgets
            if combo_id in _SKIP_COMBOBOX_IDS or any(
                c in combo_cls for c in _SKIP_COMBOBOX_CLASSES
            ):
                continue
            # Skip country-code listboxes (phone widget)
            aria_controls = combo.get_attribute("aria-controls") or ""
            if "country-listbox" in aria_controls:
                continue
            # Location autocomplete: type city, wait, click first suggestion
            label = _get_field_label(page, combo)
            combo_id_lower = combo_id.lower()
            is_location = any(
                kw in (label or "").lower() or kw in combo_id_lower for kw in _LOCATION_KEYWORDS
            )
            if is_location and profile.city:
                try:
                    if combo.input_value():
                        continue
                except Exception:
                    pass
                try:
                    combo.click()
                    combo.fill("")
                    combo.type(profile.city, delay=50)
                    page.wait_for_timeout(1500)
                    suggestion = page.query_selector(
                        "[role='option'], [class*='suggestion'], "
                        "[class*='autocomplete'] li, [class*='listbox'] li"
                    )
                    if suggestion and suggestion.is_visible():
                        _safe_click(suggestion, page)
                        log.info(
                            f"   📍 Location autocomplete '{(label or combo_id)[:40]}'"
                            f" → '{profile.city}'"
                        )
                        filled += 1
                        stats._field_fills.append(
                            {
                                "field": label or combo_id,
                                "value": profile.city,
                                "source": "location_autocomplete",
                            }
                        )
                    else:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(200)
                except ApplicationAbortError:
                    raise
                except Exception as exc:
                    log.debug("Location autocomplete failed for '%s': %s", combo_id, exc)
                continue
            # Check if already filled — React-Select input_value() is unreliable,
            # so also check for a visible single-value container or placeholder text
            try:
                if combo.input_value():
                    continue
            except Exception:
                pass
            try:
                already_filled = page.evaluate(
                    """(el) => {
                    // Walk up to the React-Select value container or control
                    // IMPORTANT: use "value-container" or "control", NOT just "container"
                    // because "select__input-container" is a narrow inner wrapper
                    let container = el.closest(
                        '[class*="value-container"], [class*="control"], '
                        + '[class*="select-shell"], [class*="-container"]:not([class*="input-container"])'
                    );
                    if (!container) container = el.parentElement?.parentElement?.parentElement;
                    if (!container) return false;
                    // Check for single-value indicator (React-Select renders a div with the selected text)
                    const singleValue = container.querySelector('[class*="singleValue"], [class*="single-value"]');
                    if (singleValue && singleValue.textContent.trim()) return true;
                    // Check if placeholder is present — if it is, the field is NOT filled
                    const placeholder = container.querySelector('[class*="placeholder"]');
                    if (placeholder) return false;
                    // No singleValue and no placeholder — ambiguous, assume not filled
                    return false;
                }""",
                    combo,
                )
                if already_filled:
                    continue
            except Exception:
                pass
            label = _get_field_label(page, combo)
            if not label:
                continue
            _check_field_label(label)
            options = _get_custom_dropdown_options(page, combo)
            if options:
                if _fill_custom_dropdown(page, combo, label, options, profile):
                    filled += 1
    except Exception as exc:
        log.debug("External custom dropdowns failed: %s", exc)

    # Div-based custom selects (Greenhouse, Lever, etc.) — these are clickable divs
    # that open a listbox but don't have role='combobox' on an input element.
    # Common patterns: [aria-haspopup='listbox'], [class*='select'][class*='control']
    try:
        custom_selects = page.query_selector_all(
            "[aria-haspopup='listbox']:not([role='combobox']), "
            "[class*='select__control'], "
            "[class*='SelectControl'], "
            "[data-testid*='select']"
        )
        for cs in custom_selects:
            try:
                if not cs.is_visible():
                    continue
                # Check if already has a selected value (no placeholder visible)
                has_value = cs.evaluate("""el => {
                    const sv = el.querySelector('[class*="singleValue"], [class*="single-value"]');
                    if (sv && sv.textContent.trim()) return true;
                    const text = el.textContent.trim();
                    if (text && !text.match(/^Select/i) && text !== '--') return true;
                    return false;
                }""")
                if has_value:
                    continue
                label = _get_field_label(page, cs)
                if not label:
                    continue
                _check_field_label(label)
                # Click to open and extract options
                _safe_click(cs, page)
                page.wait_for_timeout(400)
                opts = page.query_selector_all("[role='option']:visible")
                if not opts:
                    opts = page.query_selector_all("[role='listbox'] [role='option']")
                opt_texts = [o.inner_text().strip() for o in opts[:30] if o.inner_text().strip()]
                if opt_texts:
                    answer = _ai_answer_question(
                        f"{label} (choose one: {', '.join(opt_texts[:20])})", profile
                    )
                    if answer:
                        best_idx = _best_option_match(answer, opt_texts)
                        if best_idx >= 0:
                            _safe_click(opts[best_idx], page)
                            log.info(
                                f"   🤖 Custom select '{label[:40]}' → '{opt_texts[best_idx]}'"
                            )
                            filled += 1
                        else:
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(200)
                    else:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(200)
                else:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(200)
            except ApplicationAbortError:
                raise
            except Exception as exc:
                log.debug("Custom select failed: %s", exc)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
    except Exception as exc:
        log.debug("Div-based custom selects failed: %s", exc)

    # BambooHR Fabric UI dropdowns — fab-SelectToggle buttons with aria-haspopup
    # These use a custom component: button.fab-SelectToggle opens a fab-MenuVessel
    # with fab-MenuOption[role="menuitem"] children.  Label is in aria-label attr
    # (format: "State –Select–").
    try:
        bamboo_toggles = page.query_selector_all("button.fab-SelectToggle[aria-haspopup='true']")
        for toggle in bamboo_toggles:
            try:
                if not toggle.is_visible():
                    continue
                aria_label = toggle.get_attribute("aria-label") or ""
                # Only handle unfilled ones (placeholder contains Select)
                if "select" not in aria_label.lower():
                    continue
                # Extract field label: strip trailing placeholder like "–Select–"
                label = re.sub(
                    r"\s*[-–—]+\s*Select\s*[-–—]+\s*$", "", aria_label, flags=re.IGNORECASE
                ).strip()
                if not label:
                    continue
                _check_field_label(label)
                # Click toggle to open the menu
                _safe_click(toggle, page)
                page.wait_for_timeout(500)
                # Scope option collection to the specific menu vessel for this toggle
                menu_id = toggle.get_attribute("data-menu-id") or ""
                vessel = None
                if menu_id:
                    vessel = page.query_selector(f"#{menu_id}")
                if not vessel:
                    # Fallback: find the visible fab-MenuVessel
                    for v in page.query_selector_all(".fab-MenuVessel"):
                        if v.is_visible():
                            vessel = v
                            break
                menu_opts = (
                    vessel.query_selector_all(".fab-MenuOption[role='menuitem']")
                    if vessel
                    else page.query_selector_all(".fab-MenuOption[role='menuitem']")
                )
                opt_texts = []
                opt_elements = []
                for mo in menu_opts:
                    try:
                        t = mo.inner_text().strip()
                        if t and len(t) < 80:
                            opt_texts.append(t)
                            opt_elements.append(mo)
                    except Exception:
                        continue
                if not opt_texts:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(200)
                    continue
                # If there's a search input, use it for faster selection
                search_input = (
                    vessel.query_selector(".fab-MenuSearch__input")
                    if vessel
                    else page.query_selector(".fab-MenuSearch__input")
                )
                answer = _ai_answer_question(
                    f"{label} (choose one: {', '.join(opt_texts[:25])})",
                    profile,
                )
                if not answer:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(200)
                    continue
                # Try typing into search to narrow options
                if search_input and search_input.is_visible():
                    search_input.fill(answer[:20])
                    page.wait_for_timeout(400)
                    # Re-collect filtered options within same vessel
                    menu_opts = (
                        vessel.query_selector_all(".fab-MenuOption[role='menuitem']")
                        if vessel
                        else page.query_selector_all(".fab-MenuOption[role='menuitem']")
                    )
                    opt_texts = []
                    opt_elements = []
                    for mo in menu_opts:
                        try:
                            if not mo.is_visible():
                                continue
                            t = mo.inner_text().strip()
                            if t and len(t) < 80:
                                opt_texts.append(t)
                                opt_elements.append(mo)
                        except Exception:
                            continue
                # Click best match
                clicked = False
                best_idx = _best_option_match(answer, opt_texts)
                if best_idx >= 0:
                    _safe_click(opt_elements[best_idx], page)
                    log.info(
                        "   🤖 BambooHR dropdown '%s' → '%s'",
                        label[:40],
                        opt_texts[best_idx],
                    )
                    filled += 1
                    clicked = True
                if not clicked:
                    # Fall back: click first option if exact match failed
                    if opt_elements:
                        _safe_click(opt_elements[0], page)
                        log.info(
                            "   🤖 BambooHR dropdown '%s' → '%s' (fallback)",
                            label[:40],
                            opt_texts[0],
                        )
                        filled += 1
                    else:
                        page.keyboard.press("Escape")
                page.wait_for_timeout(300)
            except ApplicationAbortError:
                raise
            except Exception as exc:
                log.debug("BambooHR toggle '%s' failed: %s", aria_label[:30], exc)
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
    except ApplicationAbortError:
        raise
    except Exception as exc:
        log.debug("BambooHR fab-SelectToggle handler failed: %s", exc)

    # Catch-all: generic aria-haspopup dropdowns still showing placeholder text
    try:
        generic_popups = page.evaluate("""() => {
            const results = [];
            const btns = document.querySelectorAll(
                'button[aria-haspopup="true"], button[aria-haspopup="listbox"]'
            );
            for (const b of btns) {
                if (!b.offsetParent) continue;
                const text = b.textContent.trim();
                const aria = b.getAttribute('aria-label') || '';
                const combined = text + ' ' + aria;
                if (/select|choose|pick/i.test(combined) && combined.length < 120) {
                    let label = '';
                    const id = b.getAttribute('aria-labelledby') || '';
                    if (id) {
                        const lbl = document.getElementById(id);
                        if (lbl) label = lbl.textContent.trim();
                    }
                    if (!label && aria) {
                        label = aria.replace(/[-–—]+\\s*Select\\s*[-–—]+/gi, '').trim();
                    }
                    if (!label) {
                        let p = b.parentElement;
                        for (let i = 0; i < 4 && p; i++) {
                            const lbl = p.querySelector('label');
                            if (lbl) { label = lbl.textContent.trim(); break; }
                            p = p.parentElement;
                        }
                    }
                    if (label) {
                        results.push({ idx: results.length, label: label });
                    }
                }
            }
            return results.slice(0, 5);
        }""")
        for dd_info in generic_popups:
            label = dd_info.get("label", "")
            if not label:
                continue
            _check_field_label(label)
            # Re-find the button by index
            btns = page.query_selector_all(
                "button[aria-haspopup='true'], button[aria-haspopup='listbox']"
            )
            visible_btns = [b for b in btns if b.is_visible()]
            idx = dd_info.get("idx", 0)
            if idx >= len(visible_btns):
                continue
            btn = visible_btns[idx]
            _safe_click(btn, page)
            page.wait_for_timeout(500)
            opts = page.query_selector_all(
                "[role='menuitem']:visible, [role='option']:visible, [role='listbox'] li:visible"
            )
            opt_texts = []
            opt_elements = []
            for o in opts:
                try:
                    t = o.inner_text().strip()
                    if t and len(t) < 80:
                        opt_texts.append(t)
                        opt_elements.append(o)
                except Exception:
                    continue
            if opt_texts:
                answer = _ai_answer_question(
                    f"{label} (choose one: {', '.join(opt_texts[:20])})", profile
                )
                if answer:
                    best_idx = _best_option_match(answer, opt_texts)
                    if best_idx >= 0:
                        _safe_click(opt_elements[best_idx], page)
                        log.info(
                            "   🤖 Popup dropdown '%s' → '%s'",
                            label[:40],
                            opt_texts[best_idx],
                        )
                        filled += 1
                    else:
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(200)
                else:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(200)
            else:
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
    except ApplicationAbortError:
        raise
    except Exception as exc:
        log.debug("Generic popup dropdowns failed: %s", exc)

    # Text/number/email/tel/url inputs
    for inp in page.query_selector_all(
        "input[type='text'], input[type='number'], input[type='email'], "
        "input[type='tel'], input[type='url'], input:not([type]), "
        "input.artdeco-text-input--input"
    ):
        try:
            if inp.input_value():
                continue
            inp_type = (inp.get_attribute("type") or "text").lower()
            if inp_type in ("hidden", "file", "radio", "checkbox", "submit", "password", "search"):
                continue
            # Skip combobox inputs — already handled by the custom dropdown loop
            if inp.get_attribute("role") == "combobox":
                continue
            # Skip ExtJS boxselect inputs — handled by the ExtJS boxselect loop
            inp_cls = inp.get_attribute("class") or ""
            try:
                if inp.evaluate("el => !!el.closest('.x-boxselect')"):
                    continue
            except Exception:
                pass
            # Skip intl-tel-input country *search* inputs (iti__search-input),
            # but NOT the main phone input (iti__tel-input).
            inp_id = inp.get_attribute("id") or ""
            if "iti__search-input" in inp_cls or "iti-search" in inp_id:
                continue
            if not inp.is_visible():
                continue
            # Check if this input is inside an intl-tel-input container —
            # these phone widgets have their own hidden input that holds the real value
            is_iti = inp.evaluate("""el => {
                const wrapper = el.closest('.iti, [class*="intl-tel-input"], [class*="iti--"]');
                if (!wrapper) return false;
                // The actual value might be in a hidden input sibling
                const hiddenInput = wrapper.querySelector('input[type="hidden"]');
                if (hiddenInput && hiddenInput.value) return true;
                // Or check if the visible input has the phone number rendered
                // (intl-tel-input uses the same input but may not update .value properly)
                return false;
            }""")
            if is_iti:
                continue
        except Exception:
            continue

        label_text = _get_field_label(page, inp)
        if not label_text or label_text in ("type", "type type", "type type required"):
            continue
        # Skip verification/security code fields — these are filled by
        # _fetch_verification_code_from_gmail after the submit attempt.
        lt_lower = label_text.lower()
        if any(
            kw in lt_lower
            for kw in (
                "security code",
                "verification code",
                "verify code",
                "verification code was sent",
            )
        ):
            continue

        # Location fields need type-and-select for autocomplete dropdowns
        lt_lower = label_text.lower()
        if (
            any(kw in lt_lower for kw in ("location", "city", "candidate-location"))
            and profile.city
        ):
            try:
                inp.click()
                inp.fill("")
                inp.type(profile.city, delay=50)
                page.wait_for_timeout(1500)
                suggestion = page.query_selector(
                    "[role='option'], [class*='suggestion'], "
                    "[class*='autocomplete'] li, [class*='listbox'] li, "
                    "[class*='pac-item'], [class*='dropdown-item'], "
                    "[class*='results'] li, [class*='menu'] [role='option']"
                )
                if suggestion and suggestion.is_visible():
                    _safe_click(suggestion, page)
                    log.info(f"   📍 Location autocomplete '{label_text[:40]}' → '{profile.city}'")
                    filled += 1
                    stats._field_fills.append(
                        {
                            "field": label_text,
                            "value": profile.city,
                            "source": "location_autocomplete",
                        }
                    )
                    continue
                # No suggestion appeared — fall through to regular fill
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
            except ApplicationAbortError:
                raise
            except Exception as exc:
                log.debug("Location autocomplete failed for '%s': %s", label_text[:40], exc)

        try:
            _fill_input_field(inp, label_text, page, profile)
            filled += 1
        except ApplicationAbortError:
            raise
        except Exception as exc:
            log.debug("Failed to fill input '%s': %s", label_text[:40], exc)

    # Date inputs
    for inp in page.query_selector_all("input[type='date']"):
        label_text = ""
        try:
            if inp.input_value():
                continue
            label_text = _get_field_label(page, inp)
            if not label_text:
                continue
            _check_field_label(label_text)
            answer = _ai_answer_question(f"{label_text} (format: YYYY-MM-DD)", profile)
            if answer:
                inp.fill(answer)
                filled += 1
                stats._field_fills.append(
                    {"field": label_text, "value": answer, "source": "ai_date"}
                )
        except ApplicationAbortError:
            raise
        except Exception as exc:
            log.debug("Date field failed for '%s': %s", label_text, exc)

    # Contenteditable fields (rich text editors)
    for el in page.query_selector_all("[contenteditable='true']"):
        label_text = ""
        try:
            if el.inner_text().strip():
                continue
            label_text = _get_field_label(page, el)
            if not label_text:
                continue
            _check_field_label(label_text)
            answer = _ai_answer_question(
                label_text,
                profile,
                field_type="textarea",
                job_title=job_title,
                company=company,
            )
            if answer:
                el.fill(answer)
                filled += 1
        except ApplicationAbortError:
            raise
        except Exception as exc:
            log.debug("Contenteditable failed for '%s': %s", label_text, exc)

    return filled


def _find_navigation_button(page):  # noqa: C901
    """Find the Next/Continue/Submit button on an external form page.
    Returns (role, element) where role is 'submit', 'next', or 'none'."""
    # Scroll to bottom to ensure below-fold buttons are rendered/visible
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(300)
    except Exception:  # noqa: S110
        pass

    # Priority: submit > next/continue
    submit_texts = [
        "Submit Application",
        "Submit application",
        "Submit",
        "Apply",
        "Send Application",
        "Complete Application",
        "Finish",
        "Apply Now",
        "Submit & Continue",
        "Apply for this job",
        "Apply for this position",
        "Confirm",
        "Submit my application",
        "Save",
    ]
    next_texts = [
        "Next",
        "Continue",
        "Next Step",
        "Proceed",
        "Save and Continue",
        "Save & Continue",
        "Save and Next",
        "Review",
        "Review Application",
        "Preview",
        "Go to next step",
        "Save & Next",
        "Next step",
    ]

    # Skip third-party apply buttons (Indeed, LinkedIn Easy Apply, etc.)
    _THIRD_PARTY_KEYWORDS = ("indeed", "linkedin", "glassdoor")
    for text in submit_texts:
        try:
            btn = page.query_selector(f"button:has-text('{text}')")
            if btn and btn.is_visible():
                btn_text = (btn.inner_text() or "").strip().lower()
                if not any(kw in btn_text for kw in _THIRD_PARTY_KEYWORDS):
                    return ("submit", btn)
        except Exception:
            continue

    # Also check input[type='submit']
    try:
        btn = page.query_selector("input[type='submit']")
        if btn and btn.is_visible():
            return ("submit", btn)
    except Exception:
        pass

    try:
        btn = page.query_selector("button[type='submit']")
        if btn and btn.is_visible():
            text = (btn.inner_text() or "").strip().lower()
            _skip_btn_texts = ("sign in", "log in", "login", "cookie", "cookies")
            if not any(s in text for s in _skip_btn_texts):
                return ("submit", btn)
    except Exception:
        pass

    for text in next_texts:
        try:
            btn = page.query_selector(f"button:has-text('{text}')")
            if btn and btn.is_visible():
                return ("next", btn)
        except Exception:
            continue

    # Try anchor-based buttons (submit texts first, then next texts)
    for text in submit_texts:
        try:
            btn = page.query_selector(f"a:has-text('{text}')")
            if btn and btn.is_visible():
                return ("submit", btn)
        except Exception:
            continue
    for text in next_texts:
        try:
            btn = page.query_selector(f"a:has-text('{text}')")
            if btn and btn.is_visible():
                return ("next", btn)
        except Exception:  # noqa: S112
            continue

    # Try div[role='button'] (some ATS frameworks use styled divs)
    for text in submit_texts:
        try:
            btn = page.query_selector(f"div[role='button']:has-text('{text}')")
            if btn and btn.is_visible():
                return ("submit", btn)
        except Exception:  # noqa: S112
            continue
    for text in next_texts:
        try:
            btn = page.query_selector(f"div[role='button']:has-text('{text}')")
            if btn and btn.is_visible():
                return ("next", btn)
        except Exception:  # noqa: S112
            continue

    # Last resort: any visible button with submit-like aria-label
    try:
        btn = page.query_selector(
            "button[aria-label*='submit' i], "
            "button[aria-label*='apply' i], "
            "button[data-action='submit'], "
            "[role='button'][aria-label*='submit' i]"
        )
        if btn and btn.is_visible():
            return ("submit", btn)
    except Exception:  # noqa: S110
        pass

    # Web Components / Shadow DOM fallback using Playwright's role-based locator
    # (pierces custom elements like SmartRecruiters' <spl-button>)
    # Search next_texts first to avoid matching third-party apply buttons
    for text in next_texts + submit_texts:
        try:
            loc = page.get_by_role("button", name=text, exact=True)
            if loc.count() > 0 and loc.first.is_visible():
                role = "next" if text in next_texts else "submit"
                return (role, loc.first)
        except Exception:  # noqa: S112
            continue

    # AI vision fallback: screenshot the page and ask Claude to find the button
    if _AI_AVAILABLE:
        result = _ai_find_navigation_button(page)
        if result:
            return result

    return ("none", None)


def _ai_find_navigation_button(page):
    """Use Claude vision to identify a submit/next button from a screenshot."""
    import base64

    try:
        screenshot_bytes = page.screenshot(type="jpeg", quality=60)
        b64 = base64.b64encode(screenshot_bytes).decode()

        client = _get_ai_client()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=150,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "This is a job application form page. Find the primary action button "
                                "to advance (Submit, Apply, Next, Continue, etc). "
                                "Reply with ONLY the exact visible button text, or NONE if no such button exists. "
                                "Ignore login, cancel, save-for-later, and back buttons."
                            ),
                        },
                    ],
                }
            ],
        )
        stats.add_ai_tokens(response.usage)
        button_text = response.content[0].text.strip().strip('"').strip("'")
        log.info(f"   👁️ AI vision found button: '{button_text}'")

        if not button_text or button_text.upper() == "NONE":
            return None

        # Try to find the element by its text
        for selector in [
            f"button:has-text('{button_text}')",
            f"a:has-text('{button_text}')",
            f"[role='button']:has-text('{button_text}')",
            f"input[value='{button_text}']",
        ]:
            try:
                btn = page.query_selector(selector)
                if btn and btn.is_visible():
                    role = (
                        "submit"
                        if any(
                            w in button_text.lower()
                            for w in ("submit", "apply", "finish", "complete", "confirm")
                        )
                        else "next"
                    )
                    return (role, btn)
            except Exception:  # noqa: S112
                continue

        # Fallback: use page.get_by_text for fuzzy matching
        try:
            locator = page.get_by_text(button_text, exact=False)
            if locator.count() > 0:
                el = locator.first.element_handle()
                if el and el.is_visible():
                    role = (
                        "submit"
                        if any(
                            w in button_text.lower()
                            for w in ("submit", "apply", "finish", "complete", "confirm")
                        )
                        else "next"
                    )
                    return (role, el)
        except Exception:  # noqa: S110
            pass

    except Exception as exc:
        log.debug("AI vision button detection failed: %s", exc)

    return None


def _navigate_external_form(  # noqa: C901
    page,
    profile: ApplicantProfile,
    job: dict,
    cover_letter_path: str,
    owns_browser: bool,
    context,
    dry_run: bool = False,
    handler=None,
    handler_ctx: dict | None = None,
) -> str:
    """Navigate a multi-step external application form. Returns status string."""
    stalled = 0
    no_button_stalled = 0
    zero_fill_steps = 0
    prev_snapshot = ""
    fields_filled_total = 0
    captcha_solved_urls: set = set()  # track URLs where we already solved a CAPTCHA
    uploaded_files: set = set()  # track file uploads across steps to prevent re-uploads
    login_resolved = False  # set True after login/account creation succeeds once

    # Detect iframe-based forms (e.g. SmartRecruiters /oneclick-ui/)
    # If main page has no visible inputs but an iframe does, switch to that frame.
    main_inputs = page.query_selector_all("input:not([type=hidden]), textarea, select")
    if not main_inputs:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                frame_inputs = frame.query_selector_all(
                    "input:not([type=hidden]), textarea, select"
                )
                if len(frame_inputs) >= 2:
                    log.debug("Form is inside iframe (%s), switching context", frame.url[:60])
                    page = frame  # type: ignore[assignment]
                    break
            except Exception:  # noqa: S110
                pass

    for step in range(_MAX_EXTERNAL_STEPS):
        page.wait_for_timeout(1500)

        # Keep ATS URL updated as page may redirect during form flow
        stats._final_ats_url = page.url

        # Handler: per-step hook
        if handler and handler_ctx:
            step_result = handler.on_step_start(page, handler_ctx)
            if step_result:
                return step_result
            if handler_ctx.get("skip_step"):
                handler_ctx.pop("skip_step")
                continue

        # Login wall check on every step (some sites redirect mid-form)
        # Skip if we already resolved login/account for this session to prevent
        # false re-detection (e.g. Workday keeps password fields in page HTML).
        if not login_resolved and _detect_login_page(page):
            handler_resolved = (
                handler.resolve_login_wall(page, handler_ctx or {}) if handler else False
            )
            if not handler_resolved and not _resolve_login_wall(page, profile):
                log.info(f"   🔒 Requires account: {page.url[:60]}")
                return "skipped: requires account"
            login_resolved = True

        # CAPTCHA detection — attempt solve up to 2 times per page URL
        captcha_info = _detect_captcha(page)
        if captcha_info and page.url not in captcha_solved_urls:
            if profile.captcha_api_key:
                solved = False
                for captcha_attempt in range(2):
                    solved = _solve_captcha(
                        page, captcha_info, profile.captcha_api_key, profile.captcha_service
                    )
                    if solved:
                        break
                    if captcha_attempt == 0:
                        log.info("   🧩 Retrying CAPTCHA solve (attempt 2)...")
                        page.wait_for_timeout(2000)
                        captcha_info = _detect_captcha(page)
                        if not captcha_info:
                            break
                if solved:
                    captcha_solved_urls.add(page.url)
                    # Only auto-click a navigation button if this looks like a
                    # captcha-GATE page (no form fields underneath). On forms
                    # with inline captchas (Ashby, Workable, Lever, ...), the
                    # nav button IS the form's Submit -- clicking it now would
                    # submit an empty form right after the captcha token,
                    # which Ashby's spam filter (and similar) flag as bot.
                    visible_form_fields = page.query_selector_all(
                        "input[type='text']:visible, input[type='email']:visible, "
                        "input[type='tel']:visible, input[type='url']:visible, "
                        "input[type='number']:visible, textarea:visible"
                    )
                    if len(visible_form_fields) < 3:
                        _btn_role, _btn_el = _find_navigation_button(page)
                        if _btn_el:
                            _safe_click(_btn_el, page)
                            page.wait_for_timeout(3000)
                    log.info("   🧩 CAPTCHA solved, continuing form flow")
                    continue
                else:
                    log.warning("   🛡️  CAPTCHA solve failed — cannot proceed")
                    _dump_form_debug(page, job.get("id", ""), "CAPTCHA solve failed")
                    return "failed: captcha solve failed"
            else:
                log.warning("   🛡️  CAPTCHA detected but no captcha_api_key configured")
                _dump_form_debug(page, job.get("id", ""), "CAPTCHA detected")
                return "failed: captcha required"

        # Take snapshot and check for success
        snapshot = _extract_page_snapshot(page)

        handler_success = (
            handler.detect_success(page, handler_ctx) if handler and handler_ctx else False
        )
        if handler_success or _detect_success_or_confirmation(page, snapshot):
            log.info(f"   ✅ Application confirmed after {step + 1} steps")
            return "submitted"

        # Stall detection — try filling missed fields before giving up
        if snapshot == prev_snapshot:
            # Re-attempt field filling on stall (fields may have appeared after click)
            n_refilled = _answer_external_screening_questions(
                page, profile, job_title=job.get("title"), company=job.get("company")
            )
            if n_refilled > 0:
                log.debug("External form stalled but filled %d new fields, retrying", n_refilled)
                fields_filled_total += n_refilled
                stalled = 0  # reset — we made progress
            else:
                stalled += 1
                if stalled >= 3:
                    _dump_form_debug(page, job.get("id", ""), "External form stalled")
                    return f"failed: external form stuck (step {step + 1}/{_MAX_EXTERNAL_STEPS})"
        else:
            stalled = 0
        prev_snapshot = snapshot

        # Classify the page
        classification = _classify_page(snapshot, page.url)
        log.debug("   Page classification: %s", classification.get("notes", ""))

        if classification["page_type"] == "login" or classification.get("has_required_login"):
            if not (handler and handler.resolve_login_wall(page, handler_ctx or {})):
                return "skipped: requires account"

        if classification["page_type"] == "confirmation":
            return "submitted"

        if classification["page_type"] == "error":
            _dump_form_debug(page, job.get("id", ""), "Error page")
            return "failed: ATS error page"

        if classification["page_type"] == "job_search":
            # Check for Apply button before bailing -- Workday job listing pages
            # are often classified as "job_search" but have a clickable Apply button
            _has_apply = page.query_selector(
                "a[data-automation-id='jobPostingApplyButton'], "
                "a[data-automation-id='adventureButton'], "
                "button[data-automation-id='adventureButton'], "
                "a:has-text('Apply'):not(:has-text('Indeed'))"
            )
            if not _has_apply:
                log.info("   ⏭  Page is a job search/listing page, not an application form")
                return "failed: landed on job search page instead of application form"
            log.info("   🔗 Job listing page with Apply button detected, proceeding...")

        # Also catch job search pages by URL pattern (fast path, no AI needed)
        _url_lower = page.url.lower()
        if any(
            pattern in _url_lower
            for pattern in ("/jobs/search", "/search?query=", "/job-search", "kiosk+mode")
        ):
            log.info("   ⏭  URL looks like a job search page, skipping")
            return "failed: landed on job search page instead of application form"

        # Job listing page with Apply button (Workday, company career pages)
        # If no form fields and page has an Apply button, click through to the form
        if not classification.get("has_form_fields") and not classification.get("has_file_upload"):
            # Apply-button lookup must require :visible -- otherwise generic
            # :has-text('Apply') selectors can match hidden elements inside
            # cookie-consent modals (e.g. OneTrust's #filter-apply-handler),
            # which _safe_click fires via JS and navigates nowhere, stalling
            # the form loop on "Job listing page detected" forever.
            apply_btn = page.query_selector(
                "a[data-automation-id='jobPostingApplyButton']:visible, "
                "button[data-automation-id='jobPostingApplyButton']:visible, "
                "a[data-automation-id='adventureButton']:visible, "
                "button[data-automation-id='adventureButton']:visible, "
                "a:visible:has-text('Apply'):not(:has-text('Indeed')), "
                "button:visible:has-text('Apply'):not(:has-text('Indeed')), "
                "a[href*='/apply']:visible, "
                "a.css-1ixbfil:visible, "  # Workday apply button class
                "a[data-uxi-element-id='Apply']:visible"
            )
            if apply_btn:
                log.info("   🔗 Job listing page detected, clicking Apply button...")
                _safe_click(apply_btn, page)
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:  # noqa: S110
                    pass
                stats._final_ats_url = page.url
                continue  # re-enter loop on the new page

        # Detect email verification code prompt and handle it before filling fields
        if profile.gmail_app_password:
            has_verification_prompt = page.evaluate("""() => {
                const text = document.body ? document.body.innerText : '';
                const lower = text.toLowerCase();
                return lower.includes('verification code was sent')
                    || (lower.includes('security code') && lower.includes('character code'))
                    || lower.includes('enter the') && lower.includes('code to confirm');
            }""")
            if has_verification_prompt and handler and handler_ctx:
                hvc_result = handler.handle_verification_code(page, handler_ctx)
                if hvc_result:
                    if hvc_result == "submitted":
                        if owns_browser:
                            _save_session(context)
                        return "submitted"
                    if hvc_result == "continue":
                        continue
                    return hvc_result
            if has_verification_prompt:
                code = _fetch_verification_code_from_gmail(
                    profile.email, profile.gmail_app_password, max_wait=45
                )
                if code:
                    # Find the security code input — try multiple strategies
                    code_input = None
                    # Strategy 1: Playwright's label-based locator
                    for label_text in ("Security code", "Verification code", "Security Code"):
                        try:
                            loc = page.get_by_label(label_text)
                            if loc.count() > 0 and loc.first.is_visible():
                                code_input = loc.first
                                break
                        except Exception:
                            continue
                    # Strategy 2: attribute-based selector
                    if not code_input:
                        code_input = page.query_selector(
                            "input[name*='security' i], input[name*='code' i], "
                            "input[name*='verif' i], input[placeholder*='code' i], "
                            "input[aria-label*='code' i], input[aria-label*='security' i]"
                        )
                    # Strategy 3: empty visible text input near "code" label
                    if not code_input:
                        for inp in page.query_selector_all("input[type='text'], input:not([type])"):
                            try:
                                if inp.is_visible() and not inp.input_value():
                                    label = _get_field_label(page, inp)
                                    if label and "code" in label.lower():
                                        code_input = inp
                                        break
                            except Exception:
                                continue
                    if code_input:
                        # Use click + clear + type for security code inputs
                        # (React controlled components reject programmatic fill)
                        code_input.click()
                        code_input.evaluate("el => el.value = ''")
                        code_input.type(code, delay=50)
                        page.wait_for_timeout(500)
                        log.info(f"   📧 Filled verification code: {code}")
                        submit_btn = page.query_selector(
                            "button[type='submit'], input[type='submit'], button:has-text('Submit')"
                        )
                        if submit_btn:
                            _safe_click(submit_btn, page)
                        else:
                            _btn_role, _btn_el = _find_navigation_button(page)
                            if _btn_el:
                                _safe_click(_btn_el, page)
                        page.wait_for_timeout(5000)
                        post_snap = _extract_page_snapshot(page)
                        if _detect_success_or_confirmation(page, post_snap):
                            log.info("   ✅ Application submitted after verification code")
                            if owns_browser:
                                _save_session(context)
                            return "submitted"
                        log.warning("   ⚠️ Verification code may have been rejected")
                        _dump_form_debug(page, job.get("id", ""), "Verification code rejected")
                        return "failed: verification code rejected"
                else:
                    log.warning("   ⚠️ Could not retrieve verification code from email")
                    _dump_form_debug(page, job.get("id", ""), "Verification code not received")
                    return "failed: verification code not received from email"

        # Handle file uploads
        if classification.get("has_file_upload"):
            n = _handle_file_uploads(page, profile, cover_letter_path, uploaded_files)
            fields_filled_total += n
            if n > 0:
                zero_fill_steps = 0

        # Fill form fields
        if classification.get("has_form_fields") or classification["page_type"] == "form":
            n = _answer_external_screening_questions(
                page, profile, job_title=job.get("title"), company=job.get("company")
            )
            fields_filled_total += n
            if n > 0:
                stalled = 0  # filling fields counts as progress
                zero_fill_steps = 0
            else:
                zero_fill_steps += 1
                if zero_fill_steps >= 4:
                    _dump_form_debug(page, job.get("id", ""), "No progress (zero fields filled)")
                    return "failed: form not progressing (no new fields filled)"
            log.info(f"   Step {step + 1}: filled {n} fields on {page.url[:60]}")

        if dry_run:
            return "dry_run"

        # Find and click navigation button
        btn_role, btn_el = _find_navigation_button(page)

        if btn_role == "none":
            no_button_stalled += 1
            if no_button_stalled >= 3:
                _dump_form_debug(page, job.get("id", ""), "No nav button found")
                return "failed: no Next/Submit button found"
            page.wait_for_timeout(2000)
            continue

        no_button_stalled = 0
        _safe_click(btn_el, page)
        page.wait_for_timeout(2000)

        if btn_role == "submit":
            page.wait_for_timeout(3000)
            if handler and handler_ctx:
                submit_result = handler.on_submit_clicked(page, handler_ctx)
                if submit_result:
                    if submit_result == "submitted" and owns_browser:
                        _save_session(context)
                    return submit_result
            post_snapshot = _extract_page_snapshot(page)
            if _detect_success_or_confirmation(page, post_snapshot):
                if owns_browser:
                    _save_session(context)
                return "submitted"
            # Check for validation errors after submit attempt
            try:
                validation_errors = page.evaluate("""() => {
                    const errors = [];
                    // Common error selectors across ATS systems
                    const errorEls = document.querySelectorAll(
                        '[class*="error"]:not([style*="display: none"]):not(.iti__hide), '
                        + '[class*="Error"]:not([style*="display: none"]), '
                        + '[role="alert"], '
                        + '.field-error, .form-error, .invalid-feedback, '
                        + '[aria-invalid="true"]'
                    );
                    for (const el of errorEls) {
                        const text = el.textContent.trim();
                        if (text && text.length < 200 && !errors.includes(text))
                            errors.push(text);
                    }
                    return errors.slice(0, 5);
                }""")
                if validation_errors:
                    error_summary = "; ".join(validation_errors)
                    log.warning(f"   ⚠️ Validation errors: {error_summary[:200]}")
                    # Detect human verification (CAPTCHA, email verification codes)
                    es_lower = error_summary.lower()
                    email_code_patterns = (
                        "verification code",
                        "security code",
                        "verify you're a human",
                        "confirm you're a human",
                    )
                    captcha_patterns = (
                        "captcha",
                        "recaptcha",
                        "hcaptcha",
                        "flagged as possible spam",
                        "perform the security check",
                        "bot detection",
                    )
                    if any(bp in es_lower for bp in email_code_patterns):
                        # Try to fetch the code from Gmail via IMAP
                        if profile.gmail_app_password:
                            code = _fetch_verification_code_from_gmail(
                                profile.email, profile.gmail_app_password, max_wait=45
                            )
                            if code:
                                # Find the security code input
                                code_input = None
                                for lbl in ("Security code", "Verification code"):
                                    try:
                                        loc = page.get_by_label(lbl)
                                        if loc.count() > 0 and loc.first.is_visible():
                                            code_input = loc.first
                                            break
                                    except Exception:
                                        continue
                                if not code_input:
                                    code_input = page.query_selector(
                                        "input[name*='security' i], input[name*='code' i], "
                                        "input[name*='verif' i], input[placeholder*='code' i], "
                                        "input[aria-label*='code' i]"
                                    )
                                if not code_input:
                                    for inp in page.query_selector_all(
                                        "input[type='text'], input:not([type])"
                                    ):
                                        try:
                                            if inp.is_visible() and not inp.input_value():
                                                lbl = _get_field_label(page, inp)
                                                if lbl and "code" in lbl.lower():
                                                    code_input = inp
                                                    break
                                        except Exception:
                                            continue
                                if code_input:
                                    code_input.click()
                                    code_input.evaluate("el => el.value = ''")
                                    code_input.type(code, delay=50)
                                    page.wait_for_timeout(500)
                                    log.info(f"   📧 Filled verification code: {code}")
                                    # Click submit again
                                    _btn_role, _btn_el = _find_navigation_button(page)
                                    if _btn_el:
                                        _safe_click(_btn_el, page)
                                        page.wait_for_timeout(5000)
                                    # Check for success immediately
                                    post_code_snap = _extract_page_snapshot(page)
                                    if _detect_success_or_confirmation(page, post_code_snap):
                                        log.info(
                                            "   ✅ Application submitted after verification code"
                                        )
                                        if owns_browser:
                                            _save_session(context)
                                        return "submitted"
                                    # Code might be wrong/expired — bail rather than loop
                                    log.warning("   ⚠️ Verification code did not resolve the error")
                                    _dump_form_debug(
                                        page,
                                        job.get("id", ""),
                                        f"Verification code failed: {error_summary[:200]}",
                                    )
                                    return (
                                        f"failed: verification code rejected: {error_summary[:200]}"
                                    )
                                else:
                                    log.warning("   ⚠️ Got code but couldn't find input field")
                        else:
                            log.info(
                                "   ℹ️  Email verification required but no gmail_app_password "
                                "configured in profile"
                            )
                        _dump_form_debug(
                            page,
                            job.get("id", ""),
                            f"Human verification required: {error_summary[:200]}",
                        )
                        return f"failed: human verification required: {error_summary[:200]}"
                    if any(bp in es_lower for bp in captcha_patterns):
                        captcha_info = _detect_captcha(page)
                        if captcha_info and profile.captcha_api_key:
                            solved = _solve_captcha(
                                page,
                                captcha_info,
                                profile.captcha_api_key,
                                profile.captcha_service,
                            )
                            if solved:
                                page.wait_for_timeout(2000)
                                # Click submit again after solving
                                _btn_role, _btn_el = _find_navigation_button(page)
                                if _btn_el:
                                    _safe_click(_btn_el, page)
                                    page.wait_for_timeout(3000)
                                continue  # re-enter loop to check outcome
                        _dump_form_debug(
                            page,
                            job.get("id", ""),
                            f"CAPTCHA required: {error_summary[:200]}",
                        )
                        return f"failed: captcha required: {error_summary[:200]}"
                    # Jobvite "View Full Application Form" — expand to full form
                    if "view full application" in es_lower or "minimum required" in es_lower:
                        try:
                            expand_link = page.query_selector(
                                "a:has-text('View Full Application'), "
                                "a:has-text('Full Application Form'), "
                                "button:has-text('View Full Application')"
                            )
                            if expand_link and expand_link.is_visible():
                                _safe_click(expand_link, page)
                                page.wait_for_timeout(2000)
                                log.info("   🔗 Expanded to full application form")
                                continue  # re-enter loop with full form
                        except Exception:  # noqa: S110
                            pass
                    # If we can't fill any more fields, bail out
                    if fields_filled_total == 0 or stalled >= 2:
                        # Recheck — page may have navigated to login wall
                        # Handler gets first chance, then generic resolution
                        if not login_resolved and _detect_login_page(page):
                            resolved = (
                                handler.resolve_login_wall(page, handler_ctx or {})
                                if handler
                                else False
                            ) or _resolve_login_wall(page, profile)
                            if resolved:
                                login_resolved = True
                                stalled = 0
                                fields_filled_total = 0
                                continue  # retry from the new page state
                            log.info("   🔒 Requires account: %s", page.url[:60])
                            return "skipped: requires account"
                        _dump_form_debug(
                            page, job.get("id", ""), f"Validation errors: {error_summary[:200]}"
                        )
                        return f"failed: form validation errors: {error_summary[:200]}"
            except Exception:  # noqa: S110
                pass
            # Submit clicked but no confirmation — continue loop to check next state

    _dump_form_debug(page, job.get("id", ""), "Exceeded max external form steps")
    return f"failed: exceeded max form steps ({_MAX_EXTERNAL_STEPS})"


def submit_external_apply(  # noqa: C901
    job: Dict,
    profile: ApplicantProfile,
    cover_letter_path: str = "",
    proxy: Optional[str] = None,
    dry_run: bool = False,
) -> str:
    """
    Submit an external job application using AI-powered form filling.
    Works on any ATS (Workday, Greenhouse, Lever, iCIMS, etc.).
    Returns status string.
    """
    stats._apply_start_time = time.time()
    stats._field_fills.clear()
    stats._ai_answer_failures.clear()
    stats._ai_tokens_in = 0
    stats._ai_tokens_out = 0
    stats._final_ats_url = ""
    try:
        import playwright  # noqa: F401
    except ImportError:
        raise RuntimeError("Playwright not installed") from None

    # Resolve per-ATS proxy (e.g. SmartRecruiters through residential proxy)
    extra_urls = [u for u in [job.get("listing_url"), job.get("apply_url")] if u]
    effective_proxy = _resolve_proxy(job["url"], profile.proxy_rules, proxy, extra_urls)
    use_headed = effective_proxy and effective_proxy != proxy
    if use_headed:
        log.info("   🔀 Using per-ATS proxy + headed mode for %s", job["url"][:60])

    # Start virtual display for headed mode on headless servers
    xvfb_proc = None
    if use_headed and not os.environ.get("DISPLAY"):
        import subprocess

        xvfb_display = f":{random.randint(99, 199)}"
        xvfb_proc = subprocess.Popen(  # noqa: S603
            ["Xvfb", xvfb_display, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.environ["DISPLAY"] = xvfb_display
        time.sleep(0.5)
        log.debug("Started Xvfb on %s", xvfb_display)

    with _stealth_playwright() as p:
        browser, context, page, owns_browser = _playwright_context(
            p, effective_proxy, headed=use_headed
        )
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)

            # If we're still on LinkedIn, find and click the external "Apply" button
            if "linkedin.com" in page.url:
                _ensure_logged_in(page, job["url"])
                page.wait_for_timeout(2000)

                apply_sel = (
                    "a[aria-label*='Apply on company'], "
                    "a[aria-label*='Apply on external'], "
                    "button.jobs-apply-button:not(:has-text('Easy Apply')), "
                    "a.jobs-apply-button, "
                    "button:has-text('Apply'):not(:has-text('Easy'))"
                )
                # LinkedIn SPA may take several seconds to render the apply section
                apply_btn = None
                for _wait in range(4):
                    apply_btn = page.query_selector(apply_sel)
                    if apply_btn:
                        break
                    page.wait_for_timeout(2000)
                if not apply_btn:
                    # Check for LinkedIn promoted ads ("I'm interested" only, no apply)
                    interested_btn = page.query_selector(
                        'button:has-text("I\u2019m interested"), button:has-text("I\'m interested")'
                    )
                    if interested_btn:
                        return (
                            "skipped: LinkedIn promoted ad (no apply button, only 'I'm interested')"
                        )
                    # Vision-based fallback: use AI to find the Apply button
                    vision_result = _ai_find_navigation_button(page)
                    if vision_result:
                        _, apply_btn = vision_result
                    if not apply_btn:
                        _dump_form_debug(page, job.get("id", ""), "No Apply button found")
                        return "failed: no Apply button found on LinkedIn job page"

                _safe_click(apply_btn, page)
                page.wait_for_timeout(3000)

                # Handle new tab (external URLs often open in new tab)
                if len(context.pages) > 1:
                    page.close()
                    page = context.pages[-1]
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:  # noqa: S110
                        pass

            # Wait for JS rendering and dismiss cookie banners
            _wait_and_dismiss_cookies(page)

            # Capture the final ATS URL for platform detection
            stats._final_ats_url = page.url

            # Platform-specific handler
            handler = get_handler(page.url)
            handler_ctx = {
                "profile": profile,
                "job": job,
                "cover_letter_path": cover_letter_path,
            }

            # Handler pre-flight (platform-specific setup, e.g. Workday cookie banner)
            pre_flight_result = handler.pre_flight(page, handler_ctx)
            if pre_flight_result:
                return pre_flight_result

            # Update ATS URL after pre-flight may have navigated
            stats._final_ats_url = page.url

            # Login wall check: handler gets first chance, then generic resolution
            if _detect_login_page(page):
                handler_resolved = handler.resolve_login_wall(page, handler_ctx)
                if not handler_resolved and not _resolve_login_wall(page, profile):
                    log.info(f"   🔒 Skipped: requires account ({page.url[:60]})")
                    return "skipped: requires account"

            return _navigate_external_form(
                page,
                profile,
                job,
                cover_letter_path,
                owns_browser,
                context,
                dry_run=dry_run,
                handler=handler,
                handler_ctx=handler_ctx,
            )

        except ApplicationAbortError as e:
            log.warning(f"   🛡️  Application aborted: {e}")
            return f"aborted: {e}"
        except Exception as e:
            return f"failed: {e}"
        finally:
            for p_page in list(context.pages):
                try:
                    p_page.close()
                except Exception:  # noqa: S110
                    pass
            if owns_browser:
                browser.close()
            if xvfb_proc:
                xvfb_proc.terminate()
                xvfb_proc.wait()
                os.environ.pop("DISPLAY", None)
                log.debug("Stopped Xvfb")


def auto_apply_workflow(  # noqa: C901
    params: JobSearchParams,
    profile: ApplicantProfile,
    max_applications: int = 10,
    min_match_score: float = 0.30,
    dry_run: bool = False,
    proxy: Optional[str] = None,
    source: str = "linkedin",
) -> Dict:
    source_labels = {
        "linkedin": "LinkedIn",
        "remoteok": "RemoteOK",
        "hn": "HackerNews Who's Hiring",
        "biotech": "Biotech/Pharma Careers",
    }
    label = source_labels.get(source, source)
    log.info(f"🚀 {label} job apply workflow (Easy Apply + External)")
    log.info(f"   dry_run={dry_run}  max={max_applications}  min_score={min_match_score}")
    log.info(f"   AI: {'enabled' if _AI_AVAILABLE else 'disabled (install anthropic)'}\n")

    applied_urls = already_applied(load_log())
    if applied_urls:
        log.info(f"   Skipping {len(applied_urls)} already-applied jobs\n")

    log.info(f"🔍 Searching {label} for '{params.title}'...")
    try:
        if source == "remoteok":
            jobs = search_remoteok(params)
        elif source == "hn":
            jobs = search_hn_whos_hiring(params)
        elif source == "biotech":
            jobs = search_biotech(params)
        else:
            jobs = search_linkedin(params, proxy=proxy)
    except RuntimeError as e:
        log.error(f"❌ Search failed: {e}")
        return {"applications": [], "total": 0, "jobs_found": 0}

    easy_count = sum(1 for j in jobs if j.get("apply_type") == "easy_apply")
    ext_count = sum(1 for j in jobs if j.get("apply_type") == "external")
    log.info(f"✅ {len(jobs)} jobs found ({easy_count} Easy Apply, {ext_count} external)\n")
    if not jobs:
        return {"applications": [], "total": 0, "jobs_found": 0}

    applications = []
    applied = 0

    for job in jobs:
        if applied >= max_applications:
            log.info(f"✋ Reached limit ({max_applications})")
            break

        canonical_url = re.sub(r"\?.*$", "", job["url"]) if job.get("url") else ""
        if (
            job["url"] in applied_urls
            or canonical_url in applied_urls
            or job.get("id") in applied_urls
        ):
            log.info(f"⏭  Already applied: {job['title']} at {job['company']}")
            continue

        compat = ai_score_job(job, profile)

        if compat["match_score"] < min_match_score:
            reason = f" — {compat['reasoning']}" if compat.get("reasoning") else ""
            log.info(
                f"⏭  Low score ({compat['match_score']}): {job['title']} at {job['company']}{reason}"
            )
            continue

        if compat.get("deal_breakers"):
            log.info(
                f"⏭  Deal-breakers: {job['title']} at {job['company']} → {compat['deal_breakers']}"
            )
            continue

        log.info(f"✨ {job['title']} at {job['company']} (score {compat['match_score']})")
        if compat.get("reasoning"):
            log.info(f"   {compat['reasoning']}")
        if compat.get("matched_skills"):
            log.info(f"   Skills: {', '.join(compat['matched_skills'][:6])}")

        # Generate AI cover letter
        COVER_LETTER_DIR.mkdir(parents=True, exist_ok=True)
        cover_letter = ai_generate_cover_letter(job, profile)
        cl_file = _save_cover_letter_docx(cover_letter, job["id"])

        apply_type = job.get("apply_type", "easy_apply")

        if dry_run:
            log.info(f"   ⚠️  Dry run — not submitted ({apply_type})")
            status = "dry_run"
        elif apply_type == "easy_apply":
            log.info("   Submitting via Easy Apply...")
            status = submit_easy_apply(job, profile, proxy=proxy)
            icon = (
                "✅"
                if status == "submitted"
                else "🛡️ "
                if status.startswith("aborted")
                else "⚠️ "
                if "unconfirmed" in status
                else "❌"
            )
            log.info(f"   {icon} {status}")
        else:
            log.info("   Submitting via external apply...")
            status = submit_external_apply(
                job,
                profile,
                cover_letter_path=str(cl_file),
                proxy=proxy,
                dry_run=dry_run,
            )
            icon = (
                "✅"
                if status == "submitted"
                else "🔒"
                if "requires account" in status
                else "🛡️ "
                if status.startswith("aborted")
                else "⚠️ "
                if "unconfirmed" in status
                else "❌"
            )
            log.info(f"   {icon} {status}")

        # AI-generated notes for the log
        notes = ai_build_notes(job, compat)

        # Message hiring manager after successful application
        msg_status = None
        msg_text = None
        msg_poster = None
        if status.startswith("submitted") and profile.message_hiring_manager:
            try:
                msg_status, msg_text, msg_poster = _message_hiring_manager_after_apply(
                    job, profile, proxy=proxy
                )
                if msg_status:
                    log.info(f"   📨 Hiring manager message: {msg_status}")
            except Exception as exc:
                log.info("   ⚠️  Hiring manager message failed: %s", exc)

        applications.append(
            {
                "job_id": job["id"],
                "title": job["title"],
                "company": job["company"],
                "url": job["url"],
                "ats_url": stats._final_ats_url or "",
                "location": job.get("location", ""),
                "status": status,
                "apply_type": job.get("apply_type", "easy_apply"),
                "posted_ago": job.get("posted_ago", ""),
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "match_score": compat["match_score"],
                "reasoning": compat.get("reasoning", ""),
                "deal_breakers": compat.get("deal_breakers", []),
                "cover_letter_path": str(cl_file),
                "notes": notes,
                "hiring_manager_messaged": msg_status,
                "hiring_manager_message_text": msg_text,
                "hiring_manager_name": msg_poster,
                "fields_filled": list(stats._field_fills),
                "ai_answer_failures": list(stats._ai_answer_failures),
                "ai_tokens": {"input": stats._ai_tokens_in, "output": stats._ai_tokens_out},
                "cost_usd": _compute_cost_usd(stats._ai_tokens_in, stats._ai_tokens_out),
                "duration_seconds": round(time.time() - stats._apply_start_time, 1)
                if stats._apply_start_time
                else None,
                "ats_platform": _detect_ats_platform(stats._final_ats_url or job.get("url", "")),
                "failure_category": _categorize_failure(status)
                if status.startswith("failed")
                else None,
            }
        )
        # Deep-apply queue: check if failed high-match app should be re-queued
        if status.startswith("failed"):
            _queued_ids = {q["job_id"] for q in _load_deep_apply_queue()}
            app_record = applications[-1]
            if _deep_apply_eligible(app_record, _queued_ids):
                _queue_for_deep_apply(app_record)
        applied += 1
        # Human-like delay between applications (longer for Easy Apply to avoid spam flags)
        if job.get("easy_apply"):
            _human_delay(base=8.0, jitter=12.0)  # 8-20s, occasionally 20-35s
        else:
            _human_delay(base=3.0, jitter=5.0)  # 3-8s for external ATS

    save_log(applications)

    log.info("\n" + "=" * 50)
    log.info("📊 SUMMARY")
    log.info(f"   Jobs found:    {len(jobs)}")
    submitted = sum(1 for a in applications if a["status"].startswith("submitted"))
    log.info(f"   Submitted:     {submitted}")
    skipped_account = sum(1 for a in applications if a["status"] == "skipped: requires account")
    if skipped_account:
        log.info(f"   Skipped (login): {skipped_account}")
    aborted = sum(1 for a in applications if a["status"].startswith("aborted"))
    if aborted:
        log.info(f"   Aborted:       {aborted} (injection/suspicious fields detected)")
    failed = sum(1 for a in applications if a["status"].startswith("failed"))
    if failed:
        log.info(f"   Failed:        {failed}")
    log.info(f"   Log:           {LOG_FILE}")
    log.info("=" * 50)

    return {"applications": applications, "total": submitted, "jobs_found": len(jobs)}


def _run_setup():
    """Interactive setup to store LinkedIn credentials securely."""
    print("🔧 LinkedIn Easy Apply — Credential Setup")
    print(f"   Credentials will be saved to: {CREDENTIALS_FILE}")
    print("   File permissions: owner-only (0600)\n")

    existing = _load_credentials()
    if existing:
        print(f"   Existing credentials found for: {existing['email']}")
        confirm = input("   Overwrite? (y/N): ").strip().lower()
        if confirm != "y":
            print("   Keeping existing credentials.")
            return

    email = input("   LinkedIn email: ").strip()
    try:
        import getpass

        password = getpass.getpass("   LinkedIn password: ")
    except (EOFError, OSError):
        password = input("   LinkedIn password: ").strip()
    if not email or not password:
        print("   ❌ Email and password are required.")
        return

    _save_credentials(email, password)
    print(f"\n   ✅ Credentials saved to {CREDENTIALS_FILE}")
    print("   The script will now auto-login when your session expires.")


def _sync_linkedin_profile(profile_path: str) -> None:
    """Scrape the user's LinkedIn profile and update profile.json with full work history."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        raise RuntimeError("Playwright not installed") from None

    raw = json.loads(Path(profile_path).expanduser().read_text())
    profile_url = raw.get("profile", raw).get("personal", {}).get("linkedin_url")
    if not profile_url:
        log.error("❌ No linkedin_url in profile.json — can't sync")
        return

    log.info(f"🔄 Syncing profile from LinkedIn: {profile_url}")

    with _stealth_playwright() as p:
        browser, context, page, owns_browser = _playwright_context(p)
        try:
            page.goto(profile_url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3000)
            _ensure_logged_in(page, profile_url)
            page.wait_for_timeout(2000)

            # Scroll down to load all sections
            for _ in range(5):
                page.evaluate("window.scrollBy(0, 800)")
                page.wait_for_timeout(1000)

            # --- Extract work experience ---
            experience = page.evaluate("""() => {
                const items = [];
                // LinkedIn experience section
                const section = document.querySelector('#experience')
                    || document.querySelector('section:has(#experience)');
                if (!section) return items;
                const container = section.closest('section')
                    || section.parentElement?.closest('section');
                if (!container) return items;
                const entries = container.querySelectorAll(
                    'li.artdeco-list__item, '
                    + 'div[data-view-name="profile-component-entity"]'
                );
                for (const entry of entries) {
                    const spans = entry.querySelectorAll(
                        'span[aria-hidden="true"], span.visually-hidden'
                    );
                    const texts = [];
                    for (const s of spans) {
                        const t = s.innerText?.trim();
                        if (t && !texts.includes(t)) texts.push(t);
                    }
                    if (texts.length >= 2) {
                        items.push({texts: texts});
                    }
                }
                return items;
            }""")

            # --- Extract skills ---
            skills_url = profile_url.rstrip("/") + "/details/skills/"
            page.goto(skills_url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(3000)
            for _ in range(3):
                page.evaluate("window.scrollBy(0, 600)")
                page.wait_for_timeout(800)

            skills = page.evaluate("""() => {
                const items = [];
                const entries = document.querySelectorAll(
                    'span[aria-hidden="true"]'
                );
                const seen = new Set();
                for (const el of entries) {
                    const t = el.innerText?.trim();
                    if (t && t.length > 1 && t.length < 60
                        && !t.includes('\\n') && !seen.has(t)
                        && !t.match(/^\\d/)
                        && !['Show all', 'Show less', 'See all'].some(
                            x => t.startsWith(x))) {
                        seen.add(t);
                        items.push(t);
                    }
                }
                return items;
            }""")

            # --- Extract education ---
            edu_url = profile_url.rstrip("/") + "/details/education/"
            page.goto(edu_url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(2000)

            education = page.evaluate("""() => {
                const items = [];
                const entries = document.querySelectorAll(
                    'li.artdeco-list__item, '
                    + 'div[data-view-name="profile-component-entity"]'
                );
                for (const entry of entries) {
                    const spans = entry.querySelectorAll(
                        'span[aria-hidden="true"]'
                    );
                    const texts = [];
                    for (const s of spans) {
                        const t = s.innerText?.trim();
                        if (t && !texts.includes(t)) texts.push(t);
                    }
                    if (texts.length >= 1) items.push({texts: texts});
                }
                return items;
            }""")

            # --- Extract About/Summary ---
            page.goto(profile_url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(2000)

            about = page.evaluate("""() => {
                const section = document.querySelector('#about')
                    || document.querySelector('section:has(#about)');
                if (!section) return '';
                const container = section.closest('section')
                    || section.parentElement?.closest('section');
                if (!container) return '';
                const div = container.querySelector(
                    'div.display-flex span[aria-hidden="true"]'
                );
                return div ? div.innerText?.trim() : '';
            }""")

            log.info(f"   📋 Experience entries: {len(experience)}")
            log.info(f"   🛠️  Skills found: {len(skills)}")
            log.info(f"   🎓 Education entries: {len(education)}")
            log.info(f"   📝 About section: {'yes' if about else 'no'}")

            # --- Use AI to parse the raw scraped data into structured format ---
            if _AI_AVAILABLE and (experience or skills):
                client = _get_ai_client()
                parse_prompt = f"""Parse this LinkedIn profile data into structured JSON.

Raw experience entries (each has an array of text spans from the DOM):
{json.dumps(experience, indent=2)}

Raw skills list:
{json.dumps(skills[:80])}

Raw education:
{json.dumps(education, indent=2)}

About/Summary:
{about[:500] if about else "not found"}

Return ONLY valid JSON with this structure:
{{
  "previous_employers": [
    {{"title": "Job Title", "employer": "Company Name", "dates": "Start - End", "industry": "Industry if obvious", "description": "Brief 1-sentence summary of what they did"}}
  ],
  "skills": {{
    "programming_languages": ["..."],
    "frameworks": ["..."],
    "tools": ["..."]
  }},
  "education": {{
    "highest_degree": "...",
    "field_of_study": "...",
    "university": "...",
    "graduation_year": 2017
  }},
  "specializations": ["..."],
  "about": "1-2 sentence summary"
}}

Rules:
- List ALL jobs from experience, most recent first
- Categorize skills properly (languages vs frameworks vs tools)
- Keep descriptions factual and brief
- Output ONLY the JSON, no markdown fences"""

                response = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=3000,
                    messages=[{"role": "user", "content": parse_prompt}],
                )
                stats.add_ai_tokens(response.usage)
                parsed_text = response.content[0].text.strip()
                json_match = re.search(r"\{.*\}", parsed_text, re.DOTALL)
                if json_match:
                    parsed = json.loads(json_match.group())
                    _apply_synced_profile(raw, parsed, profile_path)
                else:
                    log.error("❌ AI failed to return valid JSON")
                    log.info("   Raw response: %s", parsed_text[:200])
            else:
                log.warning("⚠️  AI not available — dumping raw data for manual review")
                print(json.dumps({"experience": experience, "skills": skills}, indent=2))

        finally:
            page.close()
            if owns_browser:
                browser.close()


def _apply_synced_profile(raw: dict, parsed: dict, profile_path: str) -> None:
    """Merge AI-parsed LinkedIn data into the existing profile.json."""
    p = raw.setdefault("profile", raw)
    exp = p.setdefault("experience", {})

    # Update previous employers (keep current employer separate)
    if parsed.get("previous_employers"):
        current_employer = exp.get("current_employer", "").lower()
        prev = []
        for job in parsed["previous_employers"]:
            # Skip current employer — it's already tracked
            if current_employer and current_employer in job.get("employer", "").lower():
                # But update current title if newer data
                if job.get("title"):
                    exp["current_title"] = job["title"]
                continue
            entry = {"title": job.get("title", ""), "employer": job.get("employer", "")}
            if job.get("industry"):
                entry["industry"] = job["industry"]
            if job.get("dates"):
                entry["dates"] = job["dates"]
            if job.get("description"):
                entry["description"] = job["description"]
            prev.append(entry)
        exp["previous_employers"] = prev
        log.info(f"   ✅ Updated previous_employers: {len(prev)} entries")

    # Update skills
    if parsed.get("skills"):
        sk = p.setdefault("skills", {})
        for key in ("programming_languages", "frameworks", "tools"):
            if parsed["skills"].get(key):
                sk[key] = parsed["skills"][key]
        log.info("   ✅ Updated skills")

    # Update specializations
    if parsed.get("specializations"):
        exp["specializations"] = parsed["specializations"]
        log.info("   ✅ Updated specializations")

    # Update education
    if parsed.get("education"):
        p["education"] = parsed["education"]
        log.info("   ✅ Updated education")

    # Record sync timestamp
    p["_last_profile_sync"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    # Save
    Path(profile_path).expanduser().write_text(json.dumps(raw, indent=2))
    log.info(f"\n💾 Profile updated: {profile_path}")


PROFILE_SYNC_INTERVAL_DAYS = 7


def _maybe_sync_profile(profile_path: str) -> None:
    """Auto-sync LinkedIn profile if last sync was more than PROFILE_SYNC_INTERVAL_DAYS ago."""
    try:
        raw = json.loads(Path(profile_path).expanduser().read_text())
        last_sync = raw.get("profile", raw).get("_last_profile_sync")
        if last_sync:
            from datetime import datetime

            synced_at = datetime.strptime(last_sync, "%Y-%m-%dT%H:%M:%S")
            age_days = (datetime.now() - synced_at).days
            if age_days < PROFILE_SYNC_INTERVAL_DAYS:
                log.debug(
                    "Profile synced %d days ago (threshold=%d) — skipping",
                    age_days,
                    PROFILE_SYNC_INTERVAL_DAYS,
                )
                return
            log.info(f"📅 Profile last synced {age_days} days ago — refreshing from LinkedIn...")
        else:
            log.info("📅 Profile has never been synced — pulling from LinkedIn...")

        _sync_linkedin_profile(profile_path)
    except Exception as exc:
        log.warning(f"⚠️  Auto profile sync failed (non-critical): {exc}")


def main():  # noqa: C901
    parser = argparse.ArgumentParser(
        description="LinkedIn job apply automation (Easy Apply + External)"
    )
    parser.add_argument("--profile", default=str(DATA_DIR / "profile.json"))
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Store LinkedIn credentials for automatic login",
    )
    parser.add_argument(
        "--sync-profile",
        action="store_true",
        help="Scrape your LinkedIn profile and update profile.json with full work history/skills",
    )
    parser.add_argument(
        "--title",
        nargs="*",
        default=None,
        help="Job title(s) to search. Defaults to search_criteria.job_titles in profile.",
    )
    parser.add_argument("--location", default=None)
    parser.add_argument(
        "--remote",
        default=None,
        action="store_true",
        help="Remote only (default: from profile or True)",
    )
    parser.add_argument(
        "--no-remote", dest="remote", action="store_false", help="Include non-remote jobs"
    )
    parser.add_argument("--max-applications", type=int, default=None)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument(
        "--market-snapshot",
        action="store_true",
        default=False,
        help="Run a lightweight market scan: count total job postings per title on LinkedIn "
        "(all-time and past 2 weeks). No applications submitted. Data used by dashboard.",
    )
    parser.add_argument("--proxy", default=None)
    parser.add_argument(
        "--external-url",
        default=None,
        help="Apply to a specific external job URL (any ATS)",
    )
    parser.add_argument("--job-title", default=None, help="Job title for --external-url")
    parser.add_argument("--company", default=None, help="Company name for --external-url")
    parser.add_argument(
        "--source",
        choices=["linkedin", "remoteok", "hn", "biotech", "all"],
        default="linkedin",
        help="Job source: linkedin, remoteok, hn, biotech (pharma career sites), or all",
    )
    parser.add_argument(
        "--deep-apply",
        nargs="*",
        default=None,
        metavar="CMD",
        help="Deep-apply queue: list | prompt <job_id> | prompt-all | done <job_id> <status> [reason]",
    )
    args = parser.parse_args()

    if args.deep_apply is not None:
        cmds = args.deep_apply
        cmd = cmds[0] if cmds else "list"

        if cmd == "list":
            queue = _load_deep_apply_queue()
            pending = [q for q in queue if q["status"] == "pending"]
            if not pending:
                print("No pending deep-apply entries.")
                return
            print(f"\n{'ID':<20} {'Company':<25} {'Title':<30} {'Score':>5}  {'Failure'}")
            print("-" * 110)
            for q in pending:
                print(
                    f"{q['job_id']:<20} {q['company'][:24]:<25} "
                    f"{q['title'][:29]:<30} {q['match_score']:>5.0%}  {q['failure_reason']}"
                )
            print(f"\n{len(pending)} pending entries.")
            return

        if cmd == "prompt" and len(cmds) >= 2:
            job_id = cmds[1]
            queue = _load_deep_apply_queue()
            entry = next((q for q in queue if q["job_id"] == job_id), None)
            if not entry:
                print(f"Job ID '{job_id}' not found in deep-apply queue.")
                return
            _raw = json.loads(Path(args.profile).expanduser().read_text())
            profile = ApplicantProfile.from_dict(_raw)
            print(_generate_deep_apply_prompt(entry, profile))
            return

        if cmd == "prompt-all":
            queue = _load_deep_apply_queue()
            pending = [q for q in queue if q["status"] == "pending"]
            if not pending:
                print("No pending deep-apply entries.")
                return
            _raw = json.loads(Path(args.profile).expanduser().read_text())
            profile = ApplicantProfile.from_dict(_raw)
            for q in pending:
                print(f"\n{'=' * 80}")
                print(f"# {q['company']} — {q['title']} (ID: {q['job_id']})")
                print(f"{'=' * 80}\n")
                print(_generate_deep_apply_prompt(q, profile))
            return

        if cmd == "done" and len(cmds) >= 2:
            job_id = cmds[1]
            done_status = cmds[2] if len(cmds) >= 3 else "submitted"
            done_reason = cmds[3] if len(cmds) >= 4 else None
            ok = _mark_deep_apply_done(job_id, done_status, done_reason)
            if ok:
                print(f"Marked {job_id} as deep-apply {done_status}.")
            else:
                print(f"Job ID '{job_id}' not found in deep-apply queue.")
            return

        parser.error(
            f"Unknown deep-apply command: {cmd}. Use: list, prompt <id>, prompt-all, done <id>"
        )

    if args.setup:
        _run_setup()
        return

    if args.sync_profile:
        _sync_linkedin_profile(args.profile)
        return

    if args.market_snapshot:
        _raw = json.loads(Path(args.profile).expanduser().read_text())
        _criteria = _raw.get("search_criteria", {})
        _prefs = _raw.get("profile", _raw).get("preferences", {})
        titles = args.title if args.title else _criteria.get("job_titles", [])
        if not titles:
            parser.error(
                "No job titles — pass --title or set search_criteria.job_titles in profile"
            )
        if args.remote is None:
            work_arrangement = _prefs.get("work_arrangement", ["remote"])
            remote = "remote" in work_arrangement
        else:
            remote = args.remote
        market_snapshot(titles, location=args.location, remote=remote, proxy=args.proxy)
        return

    if args.external_url:
        _raw = json.loads(Path(args.profile).expanduser().read_text())
        profile = ApplicantProfile.from_dict(_raw)
        job = {
            "id": f"ext_{hashlib.sha256(args.external_url.encode()).hexdigest()[:12]}",
            "url": args.external_url,
            "title": args.job_title or "Unknown",
            "company": args.company or "Unknown",
            "description": "",
            "apply_type": "external",
        }
        log.info(f"🌐 Applying to external URL: {args.external_url}")
        status = submit_external_apply(
            job,
            profile,
            proxy=args.proxy,
            dry_run=args.dry_run,
        )
        log.info(f"Result: {status}")
        return

    # Auto-sync profile if stale (>7 days since last sync)
    _maybe_sync_profile(args.profile)

    _raw = json.loads(Path(args.profile).expanduser().read_text())
    profile = ApplicantProfile.from_dict(_raw)
    _settings = _raw.get("profile", _raw).get("application_settings", {})
    _criteria = _raw.get("search_criteria", {})
    _prefs = _raw.get("profile", _raw).get("preferences", {})

    max_applications = args.max_applications or _settings.get("max_applications_per_day", 10)
    min_score = (
        args.min_score if args.min_score is not None else _settings.get("min_match_score", 0.30)
    )

    # Titles: CLI args override, otherwise read from profile
    titles = args.title if args.title else _criteria.get("job_titles", [])
    if not titles:
        parser.error(
            "No job titles specified — pass --title or set search_criteria.job_titles in profile"
        )

    # Remote: CLI flag overrides, otherwise check profile preferences
    if args.remote is None:
        work_arrangement = _prefs.get("work_arrangement", ["remote"])
        remote = "remote" in work_arrangement
    else:
        remote = args.remote

    sources = ["linkedin", "remoteok", "hn", "biotech"] if args.source == "all" else [args.source]

    for source in sources:
        if len(sources) > 1:
            log.info(f"\n{'=' * 50}")
            log.info(f"📡 Source: {source.upper()}")
            log.info(f"{'=' * 50}\n")

        for i, title in enumerate(titles):
            if i > 0:
                delay = random.randint(15, 30) + (i * random.randint(3, 8))
                log.info(f"⏳ Waiting {delay}s before next search...")
                time.sleep(delay)

            params = JobSearchParams(
                title=title,
                location=args.location,
                remote=remote,
                max_age_days=_criteria.get("max_age_days", 14),
                keywords_excluded=_criteria.get("keywords_excluded", []),
                company_blacklist=_criteria.get("company_blacklist", []),
            )

            try:
                auto_apply_workflow(
                    params=params,
                    profile=profile,
                    max_applications=max_applications,
                    min_match_score=min_score,
                    dry_run=args.dry_run,
                    proxy=args.proxy,
                    source=source,
                )
            except RuntimeError as exc:
                if "session expired" in str(exc).lower():
                    log.error(
                        f"❌ Session expired during '{title}' — stopping. Re-authenticate and retry."
                    )
                    break
                log.error(f"❌ Error during '{title}': {exc}")
            continue


if __name__ == "__main__":
    main()
