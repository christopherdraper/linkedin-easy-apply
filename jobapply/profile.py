"""Applicant profile and job search parameter dataclasses."""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class JobSearchParams:
    title: str
    location: Optional[str] = None
    remote: bool = True
    max_age_days: Optional[int] = 3  # Only show jobs posted within this many days
    keywords_excluded: List[str] = field(default_factory=list)
    company_blacklist: List[str] = field(default_factory=list)


@dataclass
class ApplicantProfile:
    full_name: str
    email: str
    phone: str
    resume_path: str
    cover_letter_template: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    years_experience: Optional[int] = None
    current_title: Optional[str] = None
    current_employer: Optional[str] = None
    previous_employers: List[Dict] = field(default_factory=list)
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    country: Optional[str] = None
    skills: List[str] = field(default_factory=list)
    specializations: List[str] = field(default_factory=list)
    authorized_to_work: bool = True
    requires_sponsorship: bool = False
    screening_answers: Dict[str, str] = field(default_factory=dict)
    gmail_app_password: Optional[str] = None
    message_hiring_manager: bool = False
    education_degree: Optional[str] = None
    education_university: Optional[str] = None
    education_year: Optional[int] = None
    auto_create_accounts: bool = False
    captcha_api_key: Optional[str] = None
    captcha_service: str = "2captcha"
    proxy_rules: Dict[str, str] = field(default_factory=dict)  # ATS domain → proxy URL

    @classmethod
    def from_dict(cls, data: dict) -> "ApplicantProfile":
        p = data.get("profile", data)
        personal = p.get("personal", p)
        location = personal.get("location", {})
        work_auth = p.get("work_authorization", {})
        exp = p.get("experience", {})
        docs = p.get("documents", {})
        skills_data = p.get("skills", {})
        all_skills = (
            skills_data.get("programming_languages", [])
            + skills_data.get("frameworks", [])
            + skills_data.get("tools", [])
        )
        resume_path = docs.get("resume_path", "")
        if resume_path:
            resolved = Path(resume_path).expanduser()
            if not resolved.exists():
                log.warning(f"⚠️  Resume not found at {resolved} — file uploads will fail")

        cover_letter_template = None
        cl_path = docs.get("cover_letter_template_path")
        if cl_path:
            cl_file = Path(cl_path).expanduser()
            if cl_file.exists():
                cover_letter_template = cl_file.read_text()

        edu = p.get("education", {})

        return cls(
            full_name=personal.get("full_name", ""),
            email=personal.get("email", ""),
            phone=personal.get("phone", ""),
            resume_path=docs.get("resume_path", ""),
            cover_letter_template=cover_letter_template,
            linkedin_url=personal.get("linkedin_url"),
            github_url=personal.get("github_url"),
            years_experience=exp.get("years_total"),
            current_title=exp.get("current_title"),
            current_employer=exp.get("current_employer"),
            previous_employers=exp.get("previous_employers", []),
            city=location.get("city"),
            state=location.get("state"),
            zip_code=location.get("zip_code"),
            country=location.get("country"),
            skills=all_skills,
            specializations=exp.get("specializations", []),
            authorized_to_work=work_auth.get("authorized_to_work_us", True),
            requires_sponsorship=work_auth.get("requires_visa_sponsorship", False),
            screening_answers=p.get("screening_answers", {}),
            gmail_app_password=(
                p.get("application_settings", {}).get("gmail_app_password")
                if p.get("application_settings", {}).get("auto_fetch_verification_codes")
                else None
            ),
            message_hiring_manager=bool(
                p.get("application_settings", {}).get("message_hiring_manager")
            ),
            education_degree=edu.get("highest_degree"),
            education_university=edu.get("university"),
            education_year=edu.get("graduation_year"),
            auto_create_accounts=bool(
                p.get("application_settings", {}).get("auto_create_accounts")
            ),
            captcha_api_key=p.get("application_settings", {}).get("captcha_api_key"),
            captcha_service=p.get("application_settings", {}).get("captcha_service", "2captcha"),
            proxy_rules=p.get("application_settings", {}).get("proxy_rules", {}),
        )

    @classmethod
    def from_json(cls, path: str) -> "ApplicantProfile":
        return cls.from_dict(json.loads(Path(path).expanduser().read_text()))


def _format_previous_employers(profile: ApplicantProfile) -> str:
    if not profile.previous_employers:
        return "none listed"
    parts = []
    for pe in profile.previous_employers:
        entry = f"{pe.get('title', 'Unknown')} at {pe.get('employer', 'Unknown')}"
        if pe.get("industry"):
            entry += f" ({pe['industry']})"
        parts.append(entry)
    return "; ".join(parts)


def _profile_summary(profile: ApplicantProfile) -> str:
    """Build a text summary of the applicant for use in AI prompts."""
    location_parts = [p for p in [profile.city, profile.state] if p]
    loc_suffix = f" {profile.zip_code}" if profile.zip_code else ""
    country_line = f"\nCountry: {profile.country}" if profile.country else ""

    # Education
    edu_parts = []
    if profile.education_degree:
        edu_parts.append(profile.education_degree)
    if profile.education_university:
        edu_parts.append(profile.education_university)
    if profile.education_year:
        edu_parts.append(str(profile.education_year))
    edu_line = ", ".join(edu_parts) if edu_parts else "not provided"

    # Clearance from screening_answers
    clearance = "none"
    for key, val in profile.screening_answers.items():
        if "clearance" in key.lower() or "security" in key.lower():
            clearance = val
            break

    return f"""Name: {profile.full_name}
Email: {profile.email}
Phone: {profile.phone}
Location: {", ".join(location_parts) or "not provided"}{loc_suffix}{country_line}
LinkedIn: {profile.linkedin_url or "not provided"}
GitHub: {profile.github_url or "not provided"}
Current title: {profile.current_title or "not provided"}
Current employer: {profile.current_employer or "not provided"}
Previous roles: {_format_previous_employers(profile)}
Total years of experience: {profile.years_experience}
Specializations: {", ".join(profile.specializations)}
Skills & tools: {", ".join(profile.skills)}
Education: {edu_line}
Security clearance: {clearance}
Authorized to work in US: {profile.authorized_to_work}
Requires sponsorship: {profile.requires_sponsorship}"""


_STATE_NAMES = {
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
}
