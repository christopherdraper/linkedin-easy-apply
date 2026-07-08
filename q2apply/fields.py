"""Deterministic profile field matching and Playwright form-fill primitives."""

import logging
import re
from typing import Optional

from job_search_apply import ApplicantProfile

log = logging.getLogger(__name__)


# Map of common form label patterns -> profile attribute paths
_FIELD_MAP = {
    r"full.?name": "full_name",
    r"e.?mail": "email",
    r"phone.?country.?code|country.?code.*phone|dialing.?code": "_phone_country_code",
    r"phone|mobile|telephone": "phone",
    r"city": "city",
    r"\bstate\b|\bprovince\b": "state",
    r"zip|postal": "zip_code",
    r"country": "country",
    r"linkedin": "linkedin_url",
    r"github": "github_url",
}


def _match_field_to_profile(label: str, profile: ApplicantProfile) -> Optional[str]:
    """Try deterministic profile lookup before falling back to AI.

    Returns the value if matched, None if no match found.
    """
    label_lower = label.lower().strip()

    # Skip deterministic field matching for question-like labels (> 40 chars or
    # contains '?'). These need AI judgment, not pattern matching -- e.g.,
    # "What state will you work remotely from?" should NOT return the state abbrev.
    is_question = "?" in label_lower or len(label_lower) > 40

    field_map_matched = False

    if not is_question:
        # Profile fields first (name, email, phone, etc.)
        if profile.full_name:
            parts = profile.full_name.split(None, 1)
            if re.search(r"first.?name", label_lower) and parts:
                return parts[0]
            if re.search(r"last.?name", label_lower) and len(parts) > 1:
                return parts[1]

        for pattern, attr in _FIELD_MAP.items():
            if re.search(pattern, label_lower):
                if attr == "_phone_country_code":
                    return "+1"
                val = getattr(profile, attr, None)
                if val:
                    return str(val)
                # Pattern matched but value is empty (e.g. github_url=None).
                # Don't fall through to screening answers which may return a
                # wrong-type value (e.g. "12" years for a URL field).
                field_map_matched = True
                break

    # Screening answers match (years of X, salary, etc.)
    # Skip if the field map already claimed this label (even with empty value).
    if not field_map_matched:
        for key, val in profile.screening_answers.items():
            key_lower = key.lower()
            # Use word-boundary matching for short keys (< 10 chars) to avoid
            # "state" matching "United States" in a question about work authorization
            if len(key_lower) < 10:
                if re.search(r"\b" + re.escape(key_lower) + r"\b", label_lower):
                    # Extra guard: skip if the label is a question (contains ?) and
                    # the key is a generic field name that could appear incidentally
                    if is_question and key_lower in (
                        "state",
                        "city",
                        "country",
                        "language",
                        "english",
                        "travel",
                        "race",
                        "gender",
                        "sex",
                    ):
                        continue
                    return str(val)
            elif key_lower in label_lower or label_lower in key_lower:
                return str(val)

    return None


def _fill_react_select(page, el, ref: str, value: str) -> bool:
    """Fill a React Select / combobox component via Playwright + fiber fallback."""
    el_id = el.evaluate("e => (e.id || '').toLowerCase()")
    is_location = any(kw in el_id for kw in ("location", "city"))
    loc = page.locator(f'[data-qa-ref="{ref}"]')

    # Step 1: Try standard Playwright interaction
    loc.scroll_into_view_if_needed()
    page.wait_for_timeout(200)
    el.evaluate("e => { e.focus(); }")
    page.wait_for_timeout(200)
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(400)
    loc.press_sequentially(value, delay=60)
    page.wait_for_timeout(2500 if is_location else 1200)

    option_sel = (
        "[role='option']:not(.iti__country), [class*='select__option'], "
        "[class*='suggestion'], [class*='pac-item']"
    )
    option = page.query_selector(option_sel)
    if option:
        try:
            option.evaluate("e => e.click()")
        except Exception:
            page.keyboard.press("Enter")
        return True

    # Step 2: Fallback -- directly invoke React Select onChange via fiber tree
    ok = page.evaluate(
        """([ref, value]) => {
        const el = document.querySelector('[data-qa-ref="' + ref + '"]');
        if (!el) return false;
        const shell = el.closest('[class*="select-shell"]')
            || el.closest('[class*="select__container"]');
        if (!shell) return false;
        const fkey = Object.keys(shell).find(k =>
            k.startsWith('__reactFiber') ||
            k.startsWith('__reactInternalInstance'));
        if (!fkey) return false;
        let fiber = shell[fkey];
        for (let i = 0; i < 30 && fiber; i++) {
            const props = fiber.memoizedProps || fiber.pendingProps || {};
            if (typeof props.onChange === 'function' &&
                props.options !== undefined) {
                const match = (props.options || []).find(o =>
                    (o.label || '').toLowerCase().includes(value.toLowerCase()));
                if (match) { props.onChange(match); return true; }
                props.onChange({value: value, label: value});
                return true;
            }
            fiber = fiber.return;
        }
        return false;
    }""",
        [ref, value],
    )
    if ok:
        page.wait_for_timeout(300)
    else:
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(300)
        page.keyboard.press("Enter")
    return True


def _robust_click(el, page, timeout: int = 5000) -> None:
    """Click element with JS fallback for overlay/intercept issues (e.g. Lever ATS)."""
    try:
        el.click(timeout=timeout)
    except Exception:
        # Playwright click failed (overlay intercepts pointer events, timeout, etc.)
        # Fall back to JS click which bypasses hit-testing
        el.evaluate("e => e.click()")


_US_STATES = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "DC": "District of Columbia",
}


def _fill_pageup_combobox(page, el, value: str) -> bool:
    """Fill a PageUp-style combobox (text input with -edit/-postback pattern).

    Types into the edit field, waits for the dropdown, and clicks the matching option.
    """
    # Expand 2-letter state codes to full names for dropdown matching
    search_value = _US_STATES.get(value.upper(), value) if len(value) == 2 else value

    _robust_click(el, page)
    el.evaluate("e => { e.value = ''; }")
    page.keyboard.type(search_value, delay=50)
    page.wait_for_timeout(1500)

    # PageUp renders dropdown options as <div> or <li> items
    for sel in (
        "[class*='dropdown'] [class*='item']",
        "[class*='list'] [class*='item']",
        "[class*='autocomplete'] li",
        "[role='option']",
        "[role='listbox'] [role='option']",
    ):
        options = page.query_selector_all(sel)
        for opt in options:
            try:
                txt = (opt.text_content() or "").strip()
                if search_value.lower() in txt.lower() or txt.lower().startswith(
                    search_value.lower()
                ):
                    opt.click()
                    page.wait_for_timeout(500)
                    return True
            except Exception:
                continue

    # Fallback: arrow down + enter
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(300)
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    return True


def _fill_field(page, ref: str, value: str, field_type: str = "text") -> bool:
    """Fill a form field by its data-qa-ref attribute."""
    try:
        el = page.query_selector(f'[data-qa-ref="{ref}"]')
        if not el:
            log.warning("Element %s not found", ref)
            return False

        tag = el.evaluate("e => e.tagName.toLowerCase()")

        if tag == "select":
            el.select_option(label=value)
            return True

        input_type = el.evaluate("e => e.type || ''")
        role = el.evaluate("e => e.getAttribute('role') || ''")

        if input_type in ("checkbox", "radio"):
            if value.lower() in ("true", "yes", "1"):
                if not el.is_checked():
                    _robust_click(el, page)
            return True

        # React Select / combobox
        if role == "combobox" or el.evaluate(
            "e => e.classList.contains('select__input') || "
            "e.closest('[class*=\"select__\"]') !== null"
        ):
            return _fill_react_select(page, el, ref, value)

        # PageUp combobox (id ending in -edit with -postback sibling)
        is_pageup_combo = el.evaluate(
            "e => e.id && e.id.endsWith('-edit') && "
            "document.getElementById(e.id.replace('-edit', '-postback')) !== null"
        )
        if is_pageup_combo:
            return _fill_pageup_combobox(page, el, value)

        if tag in ("input", "textarea"):
            # Location autocomplete fields need keystroke typing to trigger
            # Google Places / Greenhouse autocomplete dropdowns
            el_label = el.evaluate(
                "e => ((e.getAttribute('aria-label') || '') + ' ' + "
                "(e.getAttribute('name') || '') + ' ' + "
                "(e.getAttribute('placeholder') || '') + ' ' + "
                "(e.getAttribute('autocomplete') || '') + ' ' + "
                "(e.getAttribute('id') || '')).toLowerCase()"
            )
            is_autocomplete_field = any(
                kw in el_label
                for kw in ("location", "candidate-location", "address-input", "pac-input")
            ) or el.evaluate(
                "e => e.closest('[data-ui=\"address\"]') !== null || "
                "e.closest('[class*=\"address-autocomplete\"]') !== null || "
                "e.getAttribute('autocomplete') === 'address-line1'"
            )
            if is_autocomplete_field:
                _robust_click(el, page)
                el.fill("")
                page.keyboard.type(value, delay=50)
                page.wait_for_timeout(1500)
                suggestion = page.query_selector(
                    "[role='option'], [class*='suggestion'], "
                    "[class*='autocomplete'] li, [class*='listbox'] li, "
                    "[class*='pac-item'], [class*='dropdown-item'], "
                    "[class*='results'] li, [class*='menu'] [role='option']"
                )
                if suggestion:
                    try:
                        if suggestion.is_visible():
                            suggestion.evaluate("e => e.click()")
                            return True
                    except Exception:
                        pass
                page.keyboard.press("ArrowDown")
                page.keyboard.press("Enter")
                return True

            _robust_click(el, page)
            el.fill("")
            el.fill(value)
            return True

        # Fallback: try clicking (for buttons, etc.)
        _robust_click(el, page)
        return True

    except Exception as e:
        log.warning("Failed to fill %s: %s", ref, str(e)[:100])
        return False


def _click_element(page, ref: str) -> bool:
    """Click an element by its data-qa-ref attribute."""
    try:
        el = page.query_selector(f'[data-qa-ref="{ref}"]')
        if not el:
            log.warning("Element %s not found for click", ref)
            return False
        _robust_click(el, page)
        return True
    except Exception as e:
        log.warning("Failed to click %s: %s", ref, str(e)[:100])
        return False


def _upload_resume(page, ref: str, resume_path: str) -> bool:
    """Upload a file to a file input element."""
    try:
        el = page.query_selector(f'[data-qa-ref="{ref}"]')
        if not el:
            log.warning("File input %s not found", ref)
            return False
        el.set_input_files(resume_path)
        return True
    except Exception as e:
        log.warning("Failed to upload to %s: %s", ref, e)
        return False
