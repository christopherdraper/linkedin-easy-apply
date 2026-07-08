"""Job scoring, cover letter generation, and application debrief notes."""

import json
import logging
import re
import time
from pathlib import Path
from typing import Dict

from jobapply import stats
from jobapply.ai import _AI_AVAILABLE, _get_ai_client
from jobapply.config import COVER_LETTER_DIR
from jobapply.profile import ApplicantProfile, _profile_summary
from jobapply.safety import _sanitize_description

log = logging.getLogger(__name__)


def score_job(job: Dict, profile: ApplicantProfile) -> Dict:
    """Keyword-based fallback scorer used when AI is unavailable."""
    description = re.sub(
        r"<[^>]+>", " ", job.get("description", "") + " " + job.get("title", "")
    ).lower()
    profile_skills = [s.lower() for s in profile.skills]
    matched = [s for s in profile_skills if s in description]
    skill_score = len(matched) / max(len(profile_skills), 1)

    # Derive title keywords from profile specializations and skills
    title_keywords = [s.lower() for s in profile.specializations] + profile_skills[:10]
    title_hits = sum(1 for kw in title_keywords if kw in job.get("title", "").lower())
    title_score = min(1.0, title_hits / 2)

    score = round((skill_score * 0.4) + (title_score * 0.6), 2)
    return {"match_score": score, "matched_skills": matched, "reasoning": "", "deal_breakers": []}


def ai_score_job(job: Dict, profile: ApplicantProfile) -> Dict:
    """
    Score a job against the applicant profile using Claude.
    Returns score (0-1), reasoning, matched skills, and any deal-breakers.
    Falls back to keyword scoring if AI is unavailable.
    """
    if not _AI_AVAILABLE:
        return score_job(job, profile)

    try:
        client = _get_ai_client()
        description = _sanitize_description(job.get("description", ""))[:4000]
        prompt = f"""{_profile_summary(profile)}

Job title: {job.get("title")}
Company: {job.get("company")}
Location: {job.get("location")}
Job description:
{description}

Rate how well this job matches the candidate. Respond with ONLY valid JSON, no other text:
{{
  "score": <0.0 to 1.0>,
  "reasoning": "<1-2 sentences: why this is or isn't a good match>",
  "matched_skills": ["<skills from their profile that appear in the job>"],
  "deal_breakers": ["<any red flags: on-site only, wrong seniority, relocation required, etc.>"]
}}

Scoring guide: 0.9+ = excellent fit, 0.7-0.9 = strong match, 0.5-0.7 = decent match, 0.3-0.5 = partial match, below 0.3 = poor fit. Be honest — don't inflate scores for weak matches.
IMPORTANT: Contract, freelance, and hourly roles are acceptable — do NOT flag them as deal-breakers. Only flag on-site-only, relocation, wrong seniority, or missing hard technical requirements.
BACKEND/SOFTWARE ENGINEERING: The candidate is open to backend software engineering roles, especially Python-heavy ones. Do NOT penalize a match just because the candidate's current title is SRE — they have strong Python skills, API development experience, and have built production automation and agentic AI systems. Score Python/backend roles based on actual skill overlap, not title mismatch.
STAFFING AGENCIES: If the company is a staffing agency, recruiting firm, or talent consultancy (not the actual employer), add "staffing_agency" to deal_breakers. Signs: company name includes words like Solutions, Staffing, Talent, Consulting, Search, Partners, Recruiting, Group; the description says "our client" or "on behalf of"; vague about the actual employer. Direct employers only — no middlemen."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        stats.add_ai_tokens(response.usage)
        raw = response.content[0].text.strip()
        # Extract JSON even if Claude wraps it in markdown code fences
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            raise ValueError(f"No JSON in response: {raw[:100]}")
        result = json.loads(json_match.group())
        result["match_score"] = round(float(result.get("score", 0.0)), 2)
        return result
    except Exception as e:
        log.warning(f"   AI scoring failed, using keyword fallback: {e}")
        return score_job(job, profile)


def _save_cover_letter_docx(text: str, job_id: str) -> Path:
    """Save cover letter text as a .docx file. Returns the file path."""
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    for line in text.split("\n"):
        doc.add_paragraph(line)
    out = COVER_LETTER_DIR / f"{job_id}.docx"
    doc.save(str(out))
    return out


def _ensure_cover_letter_docx(cl_path: str) -> str:
    """If cover letter is a .txt file, convert to .docx and return new path."""
    p = Path(cl_path)
    if not p.exists() or p.suffix != ".txt":
        return cl_path
    docx_path = p.with_suffix(".docx")
    if docx_path.exists():
        return str(docx_path)
    text = p.read_text()
    job_id = p.stem
    _save_cover_letter_docx(text, job_id)
    return str(docx_path)


def ai_generate_cover_letter(job: Dict, profile: ApplicantProfile) -> str:
    """
    Generate a personalized cover letter using Claude, guided by the template and its instructions.
    Falls back to basic template substitution if AI is unavailable.
    """
    if not _AI_AVAILABLE or not profile.cover_letter_template:
        return _basic_cover_letter(job, profile)

    try:
        client = _get_ai_client()
        # Split template body from AI instructions
        parts = profile.cover_letter_template.split("---\nTEMPLATE INSTRUCTIONS FOR AI:")
        template_body = parts[0].strip()
        instructions = parts[1].strip() if len(parts) > 1 else ""

        # Pre-fill deterministic fields so the AI can't hallucinate them
        today = time.strftime("%B %d, %Y")
        template_body = template_body.replace("{DATE}", today)
        template_body = template_body.replace("{COMPANY}", job.get("company", "the company"))
        template_body = template_body.replace("{JOB_TITLE}", job.get("title", "the role"))

        description = _sanitize_description(job.get("description", ""))[:3000]
        prompt = f"""You are writing a cover letter for a job application.

{_profile_summary(profile)}

Job title: {job.get("title")}
Company: {job.get("company")}
Job description:
{description}

Today's date: {today}

Here is the cover letter template to fill in:
{template_body}

Instructions for filling in the template:
{instructions}

Fill in every {{PLACEHOLDER}} in the template using the job description and candidate profile above. Return ONLY the completed cover letter — no preamble, no explanation, no markdown formatting.

IMPORTANT: Never use em dashes (—) or double dashes (--). Use commas, periods, or rewrite the sentence instead."""

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        stats.add_ai_tokens(response.usage)
        text = response.content[0].text.strip()
        # Strip em dashes — they scream "AI-written"
        text = (
            text.replace(" — ", ", ").replace(" -- ", ", ").replace("—", ", ").replace("--", ", ")
        )
        return text
    except Exception as e:
        log.warning(f"   AI cover letter failed, using basic template: {e}")
        return _basic_cover_letter(job, profile)


def _basic_cover_letter(job: Dict, profile: ApplicantProfile) -> str:
    """Minimal cover letter fallback when AI is unavailable."""
    if profile.cover_letter_template:
        template = profile.cover_letter_template.split("---\nTEMPLATE INSTRUCTIONS FOR AI:")[
            0
        ].strip()
        template = template.replace("{DATE}", time.strftime("%B %d, %Y"))
        template = template.replace("{COMPANY}", job.get("company", "the company"))
        template = template.replace("{JOB_TITLE}", job.get("title", "the role"))
        template = template.replace("{HIRING_MANAGER_NAME}", "there")
        return template
    specialization = (
        ", ".join(profile.specializations[:2]) if profile.specializations else "engineering"
    )
    return (
        f"{time.strftime('%B %d, %Y')}\n{job.get('company', '')}\nRE: {job.get('title', '')}\n\n"
        f"Hi there,\n\nI'm applying for the {job.get('title')} role at {job.get('company')}. "
        f"With {profile.years_experience or 'several'} years of {specialization} experience I believe I'd be a strong fit.\n\n"
        f"Please see my attached resume.\n\nThanks,\n{profile.full_name}\n"
        f"{profile.email} | {profile.phone}"
    )


def ai_build_notes(job: Dict, compat: Dict) -> str:
    """
    Write a human-readable application summary using Claude.
    Tells the applicant what they need to know if the company calls — company context,
    what the role is, what they're looking for, and any flags.
    Falls back to raw field dump if AI is unavailable.
    """
    if not _AI_AVAILABLE:
        return _basic_notes(job, compat)

    try:
        client = _get_ai_client()
        description = _sanitize_description(job.get("description", ""))[:3000]
        deal_breakers = compat.get("deal_breakers", [])
        prompt = f"""A job application was just submitted. Write a brief debrief note (3-5 sentences) so the applicant knows what he applied to if the company calls him.

Job title: {job.get("title")}
Company: {job.get("company")}
Location: {job.get("location")}
Match score: {compat.get("match_score")} — {compat.get("reasoning", "")}
Deal-breakers flagged: {deal_breakers if deal_breakers else "none"}
Job description:
{description}

Cover: what the company does, what the role involves day-to-day, what stack/tools they use, any notable details (salary, team size, on-call, growth stage, etc.). Flag anything unusual. Be direct and factual — no fluff. Plain text only."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            messages=[{"role": "user", "content": prompt}],
        )
        stats.add_ai_tokens(response.usage)
        notes = response.content[0].text.strip()
        # Append deal-breakers if any, so they're always visible
        if deal_breakers:
            notes += f"\n\n⚠️  Deal-breakers flagged: {'; '.join(deal_breakers)}"
        return notes
    except Exception as e:
        log.warning(f"   AI notes failed: {e}")
        return _basic_notes(job, compat)


def _basic_notes(job: Dict, compat: Dict) -> str:
    """Raw field dump fallback when AI is unavailable."""
    lines = [
        f"{job['title']} at {job['company']}",
        f"URL: {job['url']}",
        f"Location: {job.get('location', 'Remote')}",
    ]
    if compat.get("matched_skills"):
        lines.append(f"Skills: {', '.join(compat['matched_skills'][:8])}")
    if compat.get("reasoning"):
        lines.append(f"Reasoning: {compat['reasoning']}")
    raw = job.get("description", "")
    clean = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()
    if clean:
        snippet = (clean[:300].rsplit(" ", 1)[0] + "…") if len(clean) > 300 else clean
        lines.append(f"Description: {snippet}")
    lines.append(f"Match score: {compat['match_score']}")
    return "\n".join(lines)
