"""Per-application run state (field fills, timing, AI token usage) and
pure helpers for failure categorization, ATS platform detection, and cost."""

from typing import Dict, List

# Per-application field fill tracker — cleared at start of each application
_field_fills: List[Dict[str, str]] = []
# Per-application AI answer failure tracker — cleared alongside _field_fills
_ai_answer_failures: List[Dict[str, str]] = []
# Per-application timing — set at start of submit, read at log time
_apply_start_time: float = 0.0
# Per-application AI token usage — cleared at start of each submit
_ai_tokens_in: int = 0
_ai_tokens_out: int = 0
_final_ats_url: str = ""


def reset_run_stats() -> None:
    """Reset per-application run state at the start of a submission attempt.

    Does not touch _apply_start_time; callers set that themselves.
    """
    global _ai_tokens_in, _ai_tokens_out, _final_ats_url  # noqa: PLW0603
    _field_fills.clear()
    _ai_answer_failures.clear()
    _ai_tokens_in = 0
    _ai_tokens_out = 0
    _final_ats_url = ""


def add_ai_tokens(usage) -> None:
    """Accumulate AI token counts from an anthropic response usage object."""
    global _ai_tokens_in, _ai_tokens_out  # noqa: PLW0603
    _ai_tokens_in += usage.input_tokens
    _ai_tokens_out += usage.output_tokens


_ATS_PATTERNS = [
    # Major ATS platforms
    ("Workday", "myworkdayjobs.com"),
    ("Workday", "myworkday.com"),
    ("Workday", "wd1.myworkdaysite.com"),
    ("Workday", "wd3.myworkdaysite.com"),
    ("Workday", "wd5.myworkdaysite.com"),
    ("Greenhouse", "greenhouse.io"),
    ("Greenhouse", "boards.greenhouse.io"),
    ("Greenhouse", "job-boards.greenhouse.io"),
    # Embedded Greenhouse on a company's own careers page uses ?gh_jid=<id>
    ("Greenhouse", "gh_jid="),
    ("Greenhouse", "grnh.se"),
    ("Lever", "jobs.lever.co"),
    ("iCIMS", "icims.com"),
    ("Ashby", "ashbyhq.com"),
    # Embedded Ashby on a company's own careers page uses ?ashby_jid=<id>
    ("Ashby", "ashby_jid="),
    ("SmartRecruiters", "smartrecruiters.com"),
    ("Jobvite", "jobvite.com"),
    ("BambooHR", "bamboohr.com"),
    ("JazzHR", "applytojob.com"),
    ("JazzHR", "app.jazz.co"),
    ("Rippling", "ats.rippling.com"),
    ("Rippling", "rippling.com/company/"),
    # Mid-tier / emerging ATS
    ("Workable", "apply.workable.com"),
    ("Workable", "jobs.workable.com"),
    ("Taleo", "taleo.net"),
    ("SuccessFactors", "successfactors.com"),
    ("UltiPro", "ultipro.com"),
    ("UltiPro", "recruiting.ultipro.com"),
    ("Paylocity", "paylocity.com"),
    ("Breezy", "breezy.hr"),
    ("Recruitee", "recruitee.com"),
    ("Pinpoint", "pinpointhq.com"),
    ("Teamtailor", "teamtailor.com"),
    ("Personio", "personio.de"),
    ("Personio", "jobs.personio.com"),
    ("Comeet", "comeet.co"),
    ("Dover", "dover.com"),
    ("Kula", "kula.ai"),
    ("Kula", "careers.kula.ai"),
    ("Avature", "avature.net"),
    ("Phenom", "phenom.com"),
    ("Eightfold", "eightfold.ai"),
    ("Deel", "jobs.deel.com"),
    # Marketplace / aggregator platforms
    ("Wellfound", "wellfound.com"),
    ("Wellfound", "angel.co"),
    ("Mercor", "mercor.com"),
    ("Mercor", "work.mercor.com"),
    ("Micro1", "micro1.ai"),
    ("Micro1", "jobs.micro1.ai"),
    ("Underdog", "underdog.io"),
    ("YC Work at a Startup", "workatastartup.com"),
    ("CareerPuck", "careerpuck.com"),
    ("Click2Apply", "click2apply.net"),
    # Company-specific career portals (custom ATS)
    ("BMC Helix", "jobs.bmc.com"),
    ("Randstad", "randstaddigital.com"),
    ("Randstad", "randstad.com"),
    ("Cardinal Health", "jobs.cardinalhealth.com"),
    ("ActBlue", "actblue.com"),
    ("Oracle Recruiting", "oracle.com/careers"),
    ("Oracle Recruiting", "eeho.fa.us2.oraclecloud.com"),
]


def _detect_ats_platform(url: str) -> str:
    """Detect the ATS platform from a URL."""
    lower = url.lower()
    for name, pattern in _ATS_PATTERNS:
        if pattern in lower:
            return name
    return "unknown"


def _categorize_failure(status: str) -> str:
    """Map a freeform failure status string to a structured category."""
    s = status.lower()
    # Check captcha/spam BEFORE validation_error — spam flags often arrive
    # wrapped in a validation error message
    if "spam" in s or "captcha" in s or "security check" in s or "recaptcha" in s:
        return "captcha"
    if "form stuck" in s or "form steps" in s or "lost track" in s or "not progressing" in s:
        return "form_stuck"
    if "validation error" in s:
        return "validation_error"
    if "no apply button" in s:
        return "no_apply_button"
    if "requires account" in s or "login" in s:
        return "login_wall"
    if "modal" in s:
        return "modal_lost"
    if "max steps" in s or "too many" in s:
        return "max_steps"
    if "timeout" in s or "timed out" in s:
        return "timeout"
    return "other"


# Blended token-to-dollar rates (70% Sonnet 4.6 / 30% Haiku 4.5)
_COST_INPUT_PER_M = 2.40  # $/M input tokens
_COST_OUTPUT_PER_M = 12.00  # $/M output tokens


def _compute_cost_usd(tokens_in: int, tokens_out: int) -> float:
    """Compute estimated API cost from token counts using blended rates."""
    return round(
        (tokens_in * _COST_INPUT_PER_M / 1_000_000) + (tokens_out * _COST_OUTPUT_PER_M / 1_000_000),
        4,
    )
