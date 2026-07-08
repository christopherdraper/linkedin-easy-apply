"""External ATS applications: generic field filling, navigation-button
discovery, the multi-step form state machine, and its submit entry point."""

import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from ats_handlers import get_handler
from jobapply import stats
from jobapply.accounts import _fetch_verification_code_from_gmail
from jobapply.ai import _AI_AVAILABLE, _get_ai_client
from jobapply.browser import (
    _ensure_logged_in,
    _playwright_context,
    _resolve_proxy,
    _save_session,
    _stealth_playwright,
)
from jobapply.content import _ensure_cover_letter_docx
from jobapply.forms import (
    _ai_answer_question,
    _answer_radio_buttons,
    _answer_select_dropdowns,
    _best_option_match,
    _check_mandatory_checkboxes,
    _dump_form_debug,
    _fill_input_field,
    _get_field_label,
    _safe_click,
)
from jobapply.pages import (
    _classify_page,
    _detect_captcha,
    _detect_login_page,
    _detect_success_or_confirmation,
    _extract_page_snapshot,
    _resolve_login_wall,
    _solve_captcha,
    _wait_and_dismiss_cookies,
)
from jobapply.profile import ApplicantProfile
from jobapply.safety import ApplicationAbortError, _check_field_label

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


def _answer_yesno_button_groups(page, profile: ApplicantProfile) -> int:
    """Answer Ashby-style Yes/No button groups. Returns count of fields filled."""
    filled = 0
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
    return filled


def _answer_button_group_radios(page, profile: ApplicantProfile) -> int:
    """Answer generic button-group radio alternatives. Returns count of fields filled."""
    filled = 0
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
    return filled


def _answer_textareas(
    page, profile: ApplicantProfile, job_title: Optional[str], company: Optional[str]
) -> int:
    """Fill textareas with AI answers. Returns count of fields filled."""
    filled = 0
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
    return filled


def _answer_extjs_boxselects(page, profile: ApplicantProfile) -> int:
    """Fill ExtJS boxselect widgets. Returns count of fields filled."""
    filled = 0
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
    return filled


def _answer_custom_comboboxes(page, profile: ApplicantProfile) -> int:  # noqa: C901
    """Fill custom (ARIA combobox) dropdowns. Returns count of fields filled."""
    filled = 0
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
    return filled


def _answer_div_custom_selects(page, profile: ApplicantProfile) -> int:
    """Fill div-based custom selects. Returns count of fields filled."""
    filled = 0
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
                            stats._field_fills.append(
                                {
                                    "field": label,
                                    "value": opt_texts[best_idx][:200],
                                    "source": "ai_custom_select",
                                }
                            )
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
    return filled


def _collect_bamboo_menu_options(page, vessel, require_visible: bool):
    """Collect BambooHR fab-MenuOption texts and elements from the open menu vessel."""
    menu_opts = (
        vessel.query_selector_all(".fab-MenuOption[role='menuitem']")
        if vessel
        else page.query_selector_all(".fab-MenuOption[role='menuitem']")
    )
    opt_texts = []
    opt_elements = []
    for mo in menu_opts:
        try:
            if require_visible and not mo.is_visible():
                continue
            t = mo.inner_text().strip()
            if t and len(t) < 80:
                opt_texts.append(t)
                opt_elements.append(mo)
        except Exception:
            continue
    return opt_texts, opt_elements


def _answer_bamboohr_dropdowns(page, profile: ApplicantProfile) -> int:  # noqa: C901
    """Fill BambooHR Fabric UI dropdowns. Returns count of fields filled."""
    filled = 0
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
                opt_texts, opt_elements = _collect_bamboo_menu_options(
                    page, vessel, require_visible=False
                )
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
                    opt_texts, opt_elements = _collect_bamboo_menu_options(
                        page, vessel, require_visible=True
                    )
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
                    # No confident match: close the menu and leave the field
                    # empty rather than blindly clicking the first option
                    # (which used to submit arbitrary wrong answers).
                    log.info(
                        "   ⏭  BambooHR dropdown '%s': no option matched '%s', skipping",
                        label[:40],
                        (answer or "")[:40],
                    )
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
    return filled


def _answer_generic_popup_dropdowns(page, profile: ApplicantProfile) -> int:
    """Fill generic aria-haspopup dropdowns. Returns count of fields filled."""
    filled = 0
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
    return filled


def _answer_text_inputs(page, profile: ApplicantProfile) -> int:  # noqa: C901
    """Fill text/number/email/tel/url inputs. Returns count of fields filled."""
    filled = 0
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
    return filled


def _answer_date_inputs(page, profile: ApplicantProfile) -> int:
    """Fill date inputs with AI answers. Returns count of fields filled."""
    filled = 0
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
    return filled


def _answer_contenteditables(
    page, profile: ApplicantProfile, job_title: Optional[str], company: Optional[str]
) -> int:
    """Fill contenteditable rich text fields. Returns count of fields filled."""
    filled = 0
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


def _answer_external_screening_questions(
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

    filled += _answer_yesno_button_groups(page, profile)
    filled += _answer_button_group_radios(page, profile)

    # Native select dropdowns — reuse existing handler
    try:
        _answer_select_dropdowns(page, profile)
    except Exception as exc:
        log.debug("External select dropdowns failed: %s", exc)

    filled += _answer_textareas(page, profile, job_title, company)

    # Checkboxes — reuse existing handler
    try:
        _check_mandatory_checkboxes(page)
    except Exception as exc:
        log.debug("External checkboxes failed: %s", exc)

    filled += _answer_extjs_boxselects(page, profile)
    filled += _answer_custom_comboboxes(page, profile)
    filled += _answer_div_custom_selects(page, profile)
    filled += _answer_bamboohr_dropdowns(page, profile)
    filled += _answer_generic_popup_dropdowns(page, profile)
    filled += _answer_text_inputs(page, profile)
    filled += _answer_date_inputs(page, profile)
    filled += _answer_contenteditables(page, profile, job_title, company)

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


def _switch_to_form_iframe(page):
    """Return the frame hosting the form when the main page has no inputs."""
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
    return page


def _handle_login_wall_step(page, profile, handler, handler_ctx, login_resolved):
    """Resolve a login wall on the current step.

    Returns (status, login_resolved): status is a final status string when the
    application must stop, or None to proceed.
    """
    # Login wall check on every step (some sites redirect mid-form)
    # Skip if we already resolved login/account for this session to prevent
    # false re-detection (e.g. Workday keeps password fields in page HTML).
    if not login_resolved and _detect_login_page(page):
        handler_resolved = handler.resolve_login_wall(page, handler_ctx or {}) if handler else False
        if not handler_resolved and not _resolve_login_wall(page, profile):
            log.info(f"   🔒 Requires account: {page.url[:60]}")
            return "skipped: requires account", login_resolved
        login_resolved = True
    return None, login_resolved


def _handle_captcha_step(page, profile, job, captcha_solved_urls):
    """Detect and solve a CAPTCHA on the current step.

    Returns "continue" to re-enter the loop after a solve, a final status
    string on failure, or None to proceed with this step.
    """
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
                return "continue"
            else:
                log.warning("   🛡️  CAPTCHA solve failed — cannot proceed")
                _dump_form_debug(page, job.get("id", ""), "CAPTCHA solve failed")
                return "failed: captcha solve failed"
        else:
            log.warning("   🛡️  CAPTCHA detected but no captcha_api_key configured")
            _dump_form_debug(page, job.get("id", ""), "CAPTCHA detected")
            return "failed: captcha required"
    return None


def _check_success_and_confirmation(page, snapshot, handler, handler_ctx):
    """Return truthy when the handler or generic detection confirms submission."""
    handler_success = (
        handler.detect_success(page, handler_ctx) if handler and handler_ctx else False
    )
    return handler_success or _detect_success_or_confirmation(page, snapshot)


def _recover_from_stall(page, profile, job, stalled, fields_filled_total, step):
    """Refill fields after a stalled step.

    Returns (status, stalled, fields_filled_total): status is a final status
    string when the form is stuck, or None to proceed.
    """
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
            return (
                f"failed: external form stuck (step {step + 1}/{_MAX_EXTERNAL_STEPS})",
                stalled,
                fields_filled_total,
            )
    return None, stalled, fields_filled_total


def _classify_and_route_page(page, job, snapshot, handler, handler_ctx):  # noqa: C901
    """Classify the current page and route non-form pages.

    Returns (action, payload): ("return", status) to end with that status,
    ("continue", None) to re-enter the loop, or ("proceed", classification).
    """
    # Classify the page
    classification = _classify_page(snapshot, page.url)
    log.debug("   Page classification: %s", classification.get("notes", ""))

    if classification["page_type"] == "login" or classification.get("has_required_login"):
        if not (handler and handler.resolve_login_wall(page, handler_ctx or {})):
            return "return", "skipped: requires account"

    if classification["page_type"] == "confirmation":
        return "return", "submitted"

    if classification["page_type"] == "error":
        _dump_form_debug(page, job.get("id", ""), "Error page")
        return "return", "failed: ATS error page"

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
            return "return", "failed: landed on job search page instead of application form"
        log.info("   🔗 Job listing page with Apply button detected, proceeding...")

    # Also catch job search pages by URL pattern (fast path, no AI needed)
    _url_lower = page.url.lower()
    if any(
        pattern in _url_lower
        for pattern in ("/jobs/search", "/search?query=", "/job-search", "kiosk+mode")
    ):
        log.info("   ⏭  URL looks like a job search page, skipping")
        return "return", "failed: landed on job search page instead of application form"

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
            return "continue", None  # re-enter loop on the new page

    return "proceed", classification


def _handle_verification_step(  # noqa: C901
    page, profile, job, handler, handler_ctx, owns_browser, context
):
    """Handle an email verification code prompt before filling fields.

    Returns (action, status): ("continue", None) to re-enter the loop,
    ("return", status) to end with that status, or ("proceed", None).
    """
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
                return "return", "submitted"
            if hvc_result == "continue":
                return "continue", None
            return "return", hvc_result
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
                    return "return", "submitted"
                log.warning("   ⚠️ Verification code may have been rejected")
                _dump_form_debug(page, job.get("id", ""), "Verification code rejected")
                return "return", "failed: verification code rejected"
        else:
            log.warning("   ⚠️ Could not retrieve verification code from email")
            _dump_form_debug(page, job.get("id", ""), "Verification code not received")
            return "return", "failed: verification code not received from email"
    return "proceed", None


def _run_form_fill_phase(
    page,
    profile,
    job,
    cover_letter_path,
    classification,
    uploaded_files,
    fields_filled_total,
    zero_fill_steps,
    stalled,
    step,
):
    """Upload files and fill form fields for the current step.

    Returns (status, fields_filled_total, zero_fill_steps, stalled): status is
    a final status string when the form is not progressing, or None to proceed.
    """
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
                return (
                    "failed: form not progressing (no new fields filled)",
                    fields_filled_total,
                    zero_fill_steps,
                    stalled,
                )
        log.info(f"   Step {step + 1}: filled {n} fields on {page.url[:60]}")

    return None, fields_filled_total, zero_fill_steps, stalled


def _resubmit_with_email_code(  # noqa: C901
    page, profile, job, error_summary, owns_browser, context
):
    """Fetch an emailed verification code after submit and retry.

    Always returns a final status string.
    """
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
                for inp in page.query_selector_all("input[type='text'], input:not([type])"):
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
                    log.info("   ✅ Application submitted after verification code")
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
                return f"failed: verification code rejected: {error_summary[:200]}"
            else:
                log.warning("   ⚠️ Got code but couldn't find input field")
    else:
        log.info(
            "   ℹ️  Email verification required but no gmail_app_password configured in profile"
        )
    _dump_form_debug(
        page,
        job.get("id", ""),
        f"Human verification required: {error_summary[:200]}",
    )
    return f"failed: human verification required: {error_summary[:200]}"


def _resolve_post_submit_captcha(page, profile, job, error_summary):
    """Solve a CAPTCHA surfaced by post-submit validation errors.

    Returns "continue" after a successful solve and resubmit, or a final
    failure status string.
    """
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
            return "continue"  # re-enter loop to check outcome
    _dump_form_debug(
        page,
        job.get("id", ""),
        f"CAPTCHA required: {error_summary[:200]}",
    )
    return f"failed: captcha required: {error_summary[:200]}"


def _handle_post_submit_click(  # noqa: C901
    page,
    profile,
    job,
    handler,
    handler_ctx,
    owns_browser,
    context,
    fields_filled_total,
    stalled,
    login_resolved,
):
    """Run post-submit hooks, success detection, and validation-error recovery.

    Returns (action, status, login_resolved, stalled, fields_filled_total)
    with action one of "proceed", "continue", or "return".
    """
    if handler and handler_ctx:
        submit_result = handler.on_submit_clicked(page, handler_ctx)
        if submit_result:
            if submit_result == "submitted" and owns_browser:
                _save_session(context)
            return "return", submit_result, login_resolved, stalled, fields_filled_total
    post_snapshot = _extract_page_snapshot(page)
    if _detect_success_or_confirmation(page, post_snapshot):
        if owns_browser:
            _save_session(context)
        return "return", "submitted", login_resolved, stalled, fields_filled_total
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
                status = _resubmit_with_email_code(
                    page, profile, job, error_summary, owns_browser, context
                )
                return "return", status, login_resolved, stalled, fields_filled_total
            if any(bp in es_lower for bp in captcha_patterns):
                captcha_status = _resolve_post_submit_captcha(page, profile, job, error_summary)
                if captcha_status == "continue":
                    return "continue", None, login_resolved, stalled, fields_filled_total
                return "return", captcha_status, login_resolved, stalled, fields_filled_total
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
                        # re-enter loop with full form
                        return "continue", None, login_resolved, stalled, fields_filled_total
                except Exception:  # noqa: S110
                    pass
            # If we can't fill any more fields, bail out
            if fields_filled_total == 0 or stalled >= 2:
                # Recheck — page may have navigated to login wall
                # Handler gets first chance, then generic resolution
                if not login_resolved and _detect_login_page(page):
                    resolved = (
                        handler.resolve_login_wall(page, handler_ctx or {}) if handler else False
                    ) or _resolve_login_wall(page, profile)
                    if resolved:
                        login_resolved = True
                        stalled = 0
                        fields_filled_total = 0
                        # retry from the new page state
                        return "continue", None, login_resolved, stalled, fields_filled_total
                    log.info("   🔒 Requires account: %s", page.url[:60])
                    return (
                        "return",
                        "skipped: requires account",
                        login_resolved,
                        stalled,
                        fields_filled_total,
                    )
                _dump_form_debug(
                    page, job.get("id", ""), f"Validation errors: {error_summary[:200]}"
                )
                return (
                    "return",
                    f"failed: form validation errors: {error_summary[:200]}",
                    login_resolved,
                    stalled,
                    fields_filled_total,
                )
    except Exception:  # noqa: S110
        pass
    # Submit clicked but no confirmation — continue loop to check next state
    return "proceed", None, login_resolved, stalled, fields_filled_total


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

    page = _switch_to_form_iframe(page)

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

        login_status, login_resolved = _handle_login_wall_step(
            page, profile, handler, handler_ctx, login_resolved
        )
        if login_status:
            return login_status

        captcha_signal = _handle_captcha_step(page, profile, job, captcha_solved_urls)
        if captcha_signal == "continue":
            continue
        if captcha_signal:
            return captcha_signal

        # Take snapshot and check for success
        snapshot = _extract_page_snapshot(page)

        if _check_success_and_confirmation(page, snapshot, handler, handler_ctx):
            log.info(f"   ✅ Application confirmed after {step + 1} steps")
            return "submitted"

        # Stall detection — try filling missed fields before giving up
        if snapshot == prev_snapshot:
            stall_status, stalled, fields_filled_total = _recover_from_stall(
                page, profile, job, stalled, fields_filled_total, step
            )
            if stall_status:
                return stall_status
        else:
            stalled = 0
        prev_snapshot = snapshot

        route_action, route_payload = _classify_and_route_page(
            page, job, snapshot, handler, handler_ctx
        )
        if route_action == "return":
            return route_payload
        if route_action == "continue":
            continue
        classification = route_payload

        # Detect email verification code prompt and handle it before filling fields
        if profile.gmail_app_password:
            verif_action, verif_status = _handle_verification_step(
                page, profile, job, handler, handler_ctx, owns_browser, context
            )
            if verif_action == "continue":
                continue
            if verif_action == "return":
                return verif_status

        fill_status, fields_filled_total, zero_fill_steps, stalled = _run_form_fill_phase(
            page,
            profile,
            job,
            cover_letter_path,
            classification,
            uploaded_files,
            fields_filled_total,
            zero_fill_steps,
            stalled,
            step,
        )
        if fill_status:
            return fill_status

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
            (
                submit_action,
                submit_status,
                login_resolved,
                stalled,
                fields_filled_total,
            ) = _handle_post_submit_click(
                page,
                profile,
                job,
                handler,
                handler_ctx,
                owns_browser,
                context,
                fields_filled_total,
                stalled,
                login_resolved,
            )
            if submit_action == "continue":
                continue
            if submit_action == "return":
                return submit_status

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
    # Only timing is set here. Per-application counters are reset by the
    # caller (auto_apply_workflow / cli) BEFORE scoring, so scoring and
    # cover-letter tokens land in this application's log entry.
    stats._apply_start_time = time.time()
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
