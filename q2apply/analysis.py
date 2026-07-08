"""AI page analysis: snapshot extraction and Claude-driven action planning."""

import json
import logging
import re
from typing import Dict, List, Optional

import anthropic

from job_search_apply import ApplicantProfile
from q2apply.config import MODEL

log = logging.getLogger(__name__)


_PAGE_ANALYSIS_SYSTEM = """You are an expert at completing online job application forms.
You will receive a JSON array of interactive form elements on the page. Each element has:
- "ref": element reference (e.g. "E1", "E23") - use this exact string in your response
- "tag": HTML tag (input, select, textarea, button)
- "type": input type (text, email, tel, file, checkbox, radio, submit)
- "label": the field label text
- "value": current value (already filled if non-empty)
- "text": button/element text content
- "required": whether the field is required
- "checked": whether checkbox/radio is checked
- "options": dropdown options (for select elements)

Return a JSON array of actions. Each action object must have:
- "action": one of "fill", "select", "click", "upload", "skip"
- "ref": the exact element reference from the snapshot (e.g. "E5")
- "target": human-readable description of the element
- "value": the value to fill/select (empty for click/skip)
- "reasoning": brief explanation of why
- "confidence": "high", "medium", or "uncertain"

Rules:
- Fill form fields with applicant profile data
- SKIP fields that already have the correct value (check the "value" field)
- For dropdowns/selects, pick the closest matching option from available choices
- For file inputs (type=file), use "upload" action
- For radio buttons / checkboxes, use "click" action on the correct option
- Do NOT include Submit/Next button clicks in your response - only fill form fields
- Skip fields you cannot determine a value for
- Return ONLY the JSON array, no other text
- Process fields in top-to-bottom order as they appear on the page

CRITICAL -- read the question carefully and answer what is actually being asked:
- "Are you authorized to work in the US?" is a YES/NO question, answer "Yes" -- NOT a state name
- "Do you require visa sponsorship?" is a YES/NO question, answer "No" -- NOT a location
- "Do you reside in the United States?" is a YES/NO question, answer "Yes" -- NOT a state
- Questions asking about programming languages want LANGUAGE NAMES (Python, Go, etc.) not spoken languages
- If a question asks about experience with a technology, describe relevant experience from the profile
- Do NOT confuse profile location fields (state, city) with question answers -- "Indiana" is NOT a valid answer to a yes/no question"""


def _ai_analyze_page(
    snapshot: str,
    profile: ApplicantProfile,
    job_title: str,
    company: str,
) -> List[Dict]:
    """Use Claude to analyze a page snapshot and return planned actions."""
    client = anthropic.Anthropic()

    profile_summary = _build_profile_context(profile)

    prompt = f"""Applicant profile:
{profile_summary}

Applying for: {job_title} at {company}

Page accessibility snapshot:
{snapshot}

Analyze the form fields on this page and return a JSON array of actions to complete them."""

    response = client.messages.create(
        model=MODEL,
        thinking={"type": "disabled"},
        max_tokens=2048,
        system=_PAGE_ANALYSIS_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    # Extract JSON array from response (may be wrapped in markdown code block)
    json_match = re.search(r"\[.*\]", text, re.DOTALL)
    if not json_match:
        log.warning("AI returned no valid JSON actions")
        return []

    try:
        actions = json.loads(json_match.group())
        return actions if isinstance(actions, list) else []
    except json.JSONDecodeError:
        log.warning("Failed to parse AI action JSON")
        return []


def _build_profile_context(profile: ApplicantProfile) -> str:
    """Build a concise profile summary for AI prompts."""
    lines = []
    for attr in [
        "full_name",
        "email",
        "phone",
        "city",
        "state",
        "zip_code",
        "country",
        "linkedin_url",
        "github_url",
        "current_title",
        "current_employer",
    ]:
        val = getattr(profile, attr, None)
        if val:
            lines.append(f"- {attr.replace('_', ' ').title()}: {val}")

    if profile.years_experience:
        lines.append(f"- Total years experience: {profile.years_experience}")

    if profile.screening_answers:
        lines.append("\nScreening answers:")
        for k, v in profile.screening_answers.items():
            lines.append(f"  - {k}: {v}")

    return "\n".join(lines)


def _ai_answer_field(
    label: str,
    field_type: str,
    options: List[str],
    profile: ApplicantProfile,
    job_title: str,
    company: str,
) -> Optional[str]:
    """Use Claude to answer a single form field that deterministic matching missed."""
    client = anthropic.Anthropic()

    options_str = ""
    if options:
        options_str = f"\nAvailable options: {', '.join(options)}"

    prompt = f"""Applicant profile:
{_build_profile_context(profile)}

Applying for: {job_title} at {company}

Field label: "{label}"
Field type: {field_type}{options_str}

Rules:
- "years of experience with X": single whole number
- Yes/No questions: just "Yes" or "No"
- For dropdowns, pick the exact option text from available options
- Output ONLY the value. No quotes, no units, no explanation."""

    response = client.messages.create(
        model=MODEL,
        thinking={"type": "disabled"},
        max_tokens=50,
        system="You fill job application forms. Output ONLY the answer value, nothing else.",
        messages=[{"role": "user", "content": prompt}],
    )
    answer = response.content[0].text.strip()
    return answer if answer else None


def _get_page_text_snapshot(page) -> str:
    """Get a simplified text snapshot of interactive form elements on the page."""
    return page.evaluate("""() => {
        const els = document.querySelectorAll(
            'input:not([type="hidden"]), select, textarea, ' +
            'button, [role="button"], [role="combobox"], ' +
            '[role="radio"], [role="checkbox"], [role="option"]'
        );
        const items = [];
        let refCounter = 0;
        for (const el of els) {
            if (el.offsetParent === null && el.getAttribute('type') !== 'file') continue;
            refCounter++;
            const ref = 'E' + refCounter;
            el.setAttribute('data-qa-ref', ref);
            const tag = el.tagName.toLowerCase();
            const type = el.getAttribute('type') || '';
            const role = el.getAttribute('role') || '';
            const name = el.getAttribute('name') || '';

            // Build label: check for associated <label>, aria-label, placeholder
            let label = el.getAttribute('aria-label') || '';
            if (!label) {
                const id = el.getAttribute('id');
                if (id) {
                    const labelEl = document.querySelector('label[for="' + id + '"]');
                    if (labelEl) label = labelEl.textContent.trim().substring(0, 100);
                }
            }
            if (!label) label = el.getAttribute('placeholder') || '';
            if (!label && el.closest('label')) {
                label = el.closest('label').textContent.trim().substring(0, 100);
            }

            const value = el.value || '';
            const text = el.textContent?.trim().substring(0, 100) || '';
            const required = el.hasAttribute('required') ||
                el.getAttribute('aria-required') === 'true';
            const checked = el.checked || false;

            let options = [];
            if (tag === 'select') {
                options = Array.from(el.options).map(o => o.text.trim());
            }

            items.push({
                ref, tag, type, role, name, label, value, text, required, checked,
                options: options.length > 0 ? options : undefined,
            });
        }
        return JSON.stringify(items);
    }""")
