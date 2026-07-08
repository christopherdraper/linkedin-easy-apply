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
from jobapply.safety import (
    _ABORT_FIELD_PATTERNS,
    _INJECTION_PATTERNS,
    ApplicationAbortError,
    _check_field_label,
    _looks_like_injection,
    _sanitize_description,
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
    "_answer_radio_buttons",
    "_answer_screening_questions",
    "_answer_select_dropdowns",
    "_answer_textareas",
    "_attempt_account_creation",
    "_attempt_ats_login",
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
    "_detect_ats_platform",
    "_detect_captcha",
    "_detect_login_page",
    "_detect_success_or_confirmation",
    "_determine_radio_answer",
    "_dismiss_all_typeaheads",
    "_dismiss_linkedin_overlays",
    "_dismiss_typeahead",
    "_dump_form_debug",
    "_ensure_logged_in",
    "_extract_code_from_email_body",
    "_extract_page_snapshot",
    "_fetch_verification_code_from_gmail",
    "_fill_empty_required_fields",
    "_fill_input_field",
    "_fill_registration_form",
    "_format_previous_employers",
    "_generate_ats_password",
    "_get_ai_client",
    "_get_domain",
    "_get_field_label",
    "_get_form_container",
    "_get_validation_errors",
    "_handle_registration_verification",
    "_human_delay",
    "_inject_captcha_token",
    "_inject_session_cookies_if_needed",
    "_load_ats_accounts",
    "_load_credentials",
    "_login_linkedin",
    "_looks_like_injection",
    "_match_screening_answer",
    "_playwright_context",
    "_profile_summary",
    "_resolve_login_wall",
    "_resolve_proxy",
    "_safe_click",
    "_sanitize_description",
    "_save_ats_account",
    "_save_credentials",
    "_save_session",
    "_solve_captcha",
    "_stealth_playwright",
    "_try_guest_bypass",
    "_try_resume_upload_bypass",
    "_wait_and_dismiss_cookies",
]

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)


def _fetch_description(context, url: str) -> tuple:
    """
    Fetch the full job description and posting age from a LinkedIn job page.
    Returns (description, posted_ago) where posted_ago is e.g. "2 days ago".
    """
    desc_page = context.new_page()
    try:
        desc_page.goto(url, wait_until="domcontentloaded", timeout=20000)
        desc_page.wait_for_timeout(2000)

        # Extract posting age (e.g. "2 days ago", "1 week ago", "Reposted 3 days ago")
        posted_ago = (
            desc_page.evaluate("""() => {
            const text = document.body.innerText;
            const m = text.match(/(?:Reposted\\s+)?(\\d+\\s+(?:hour|day|week|month)s?\\s+ago)/i);
            return m ? m[1] : '';
        }""")
            or ""
        )

        text = desc_page.evaluate("""() => {
            // Authenticated view: find the 'About the job' heading
            const all = [...document.querySelectorAll('*')];
            for (const el of all) {
                if (el.children.length === 0 && el.innerText?.trim() === 'About the job') {
                    let container = el;
                    for (let i = 0; i < 5; i++) {
                        if (!container.parentElement) break;
                        if (container.nextElementSibling) break;
                        container = container.parentElement;
                    }
                    let parts = [];
                    let node = container.nextElementSibling || container.parentElement?.nextElementSibling;
                    while (node && parts.join(' ').length < 5000) {
                        parts.push(node.innerText?.trim() || '');
                        node = node.nextElementSibling;
                    }
                    const result = parts.join(' ').trim();
                    if (result.length > 100) return result;
                }
            }
            // Public/guest view: description in .description__text or .show-more-less-html
            const pubDesc = document.querySelector(
                '.description__text .show-more-less-html__markup, '
                + '.show-more-less-html__markup, '
                + '.description__text'
            );
            if (pubDesc) {
                const t = pubDesc.innerText?.trim();
                if (t && t.length > 50) return t.substring(0, 5000);
            }
            // Fallback: grab body text after 'About the job' marker
            const body = document.body.innerText;
            const idx = body.indexOf('About the job');
            if (idx > -1) return body.slice(idx + 14, idx + 5000).trim();
            return '';
        }""")

        if text:
            return re.sub(r"\s+", " ", text).strip(), posted_ago
    except Exception as exc:
        log.debug("Description fetch failed for %s: %s", url, exc)
    finally:
        desc_page.close()
    return "", ""


def _parse_job_cards(page) -> List[Dict]:
    """Extract job data from visible job cards on the search results page.

    Supports both authenticated LinkedIn (div.job-card-container) and
    public/guest LinkedIn (div.job-search-card) page layouts.
    """
    try:
        cards = page.query_selector_all("div.job-card-container")
        is_public = False
        if not cards:
            # Public/guest view uses different card selectors
            cards = page.query_selector_all("div.job-search-card")
            is_public = True
        if not cards:
            return []
    except Exception:
        raise RuntimeError(
            "LinkedIn session expired -- page context destroyed (likely auth redirect)"
        ) from None

    jobs = []
    for card in cards[:25]:
        try:
            if is_public:
                title_el = card.query_selector("h3.base-search-card__title")
                link_el = card.query_selector("a.base-card__full-link")
                company_el = card.query_selector("h4.base-search-card__subtitle")
                location_el = card.query_selector(".job-search-card__location")
                easy_apply_el = card.query_selector(".job-search-card__easy-apply-label")
                has_easy_apply = easy_apply_el is not None
                href = (
                    link_el.evaluate("el => el.href || el.getAttribute('href') || ''")
                    if link_el
                    else ""
                )
            else:
                title_el = card.query_selector("a.job-card-list__title--link")
                link_el = title_el
                company_el = card.query_selector("div.artdeco-entity-lockup__subtitle")
                location_el = card.query_selector("div.artdeco-entity-lockup__caption")
                footer_items = card.query_selector_all("li.job-card-container__footer-item")
                has_easy_apply = any("easy apply" in el.inner_text().lower() for el in footer_items)
                href = (
                    title_el.evaluate("el => el.href || el.getAttribute('href') || ''")
                    if title_el
                    else ""
                )

            if title_el and company_el:
                # Ensure absolute URL
                if href and href.startswith("/"):
                    href = "https://www.linkedin.com" + href
                # Strip tracking params for stable job IDs
                canonical_href = re.sub(r"\?.*$", "", href) if href else ""
                apply_type = "easy_apply" if has_easy_apply else "external"
                jobs.append(
                    {
                        "id": f"li_{hashlib.sha256((canonical_href or title_el.inner_text()).encode()).hexdigest()[:12]}",
                        "title": title_el.inner_text().strip(),
                        "company": company_el.inner_text().strip(),
                        "location": location_el.inner_text().strip() if location_el else "",
                        "url": href,
                        "description": "",
                        "apply_type": apply_type,
                    }
                )
        except Exception as exc:
            log.debug("Skipping malformed job card: %s", exc)
            continue
    return jobs


def search_remoteok(params: JobSearchParams) -> List[Dict]:
    """
    Search RemoteOK's public JSON API for matching jobs.
    No auth required. Returns jobs in the same dict format as search_linkedin.
    """
    import urllib.request

    tag = params.title.lower().replace(" ", "-")
    api_url = f"https://remoteok.com/api?tag={tag}"
    req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        log.warning(f"   RemoteOK API error: {e}")
        return []

    # First item is metadata, skip it
    listings = data[1:] if len(data) > 1 else []

    jobs = []
    blacklist_lower = [c.lower() for c in params.company_blacklist]
    excluded_lower = [kw.lower() for kw in params.keywords_excluded]

    for item in listings:
        company = item.get("company", "")
        title = item.get("position", "")
        desc = item.get("description", "")
        url = item.get("url", "")
        apply_url = item.get("apply_url") or url

        if not apply_url or not title:
            continue

        # Apply blacklist
        if company.lower() in blacklist_lower:
            continue

        # Apply keyword exclusions
        title_lower = title.lower()
        if any(kw in title_lower for kw in excluded_lower):
            continue

        # Age filter
        if params.max_age_days:
            date_str = item.get("date", "")
            if date_str:
                try:
                    from datetime import datetime, timezone

                    posted = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    age_days = (datetime.now(timezone.utc) - posted).days
                    if age_days > params.max_age_days:
                        continue
                except Exception:
                    pass

        if url and not url.startswith("http"):
            url = f"https://remoteok.com{url}"
        if apply_url and not apply_url.startswith("http"):
            apply_url = f"https://remoteok.com{apply_url}"

        jobs.append(
            {
                "id": f"rok_{item.get('id', '')}",
                "url": apply_url,
                "listing_url": url,
                "title": title,
                "company": company,
                "description": _sanitize_description(desc[:5000]),
                "location": "Remote",
                "easy_apply": False,
                "apply_type": "external",
                "source": "remoteok",
            }
        )

    return jobs


def search_hn_whos_hiring(params: JobSearchParams) -> List[Dict]:
    """
    Search HackerNews 'Who is hiring?' threads via the Algolia API.
    Finds the current month's thread, fetches comments, and filters for relevant jobs.
    """
    import urllib.request

    # Find the most recent "Who is hiring?" thread (sort by date to get current month)
    search_url = (
        "https://hn.algolia.com/api/v1/search_by_date?"
        "query=%22who%20is%20hiring%22&tags=story"
        "&hitsPerPage=5"
    )
    req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        log.warning(f"   HN Algolia search error: {e}")
        return []

    # Find the thread from this month or last month
    thread_id = None
    for hit in data.get("hits", []):
        title = hit.get("title", "").lower()
        if "who is hiring?" in title and "freelancer" not in title and "show hn" not in title:
            thread_id = hit.get("objectID")
            log.info(f"   Found HN thread: {hit.get('title')} (id: {thread_id})")
            break

    if not thread_id:
        log.warning("   Could not find a recent 'Who is hiring?' thread")
        return []

    # Fetch all comments from the thread
    comments_url = (
        f"https://hn.algolia.com/api/v1/search?tags=comment,story_{thread_id}&hitsPerPage=500"
    )
    req = urllib.request.Request(comments_url, headers={"User-Agent": "Mozilla/5.0"})

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        log.warning(f"   HN comments fetch error: {e}")
        return []

    comments = data.get("hits", [])
    log.info(f"   Fetched {len(comments)} comments from HN thread")

    # Filter comments for relevant jobs
    search_terms = [
        t.lower()
        for t in (
            params.title.split()
            + ["sre", "devops", "platform", "infrastructure", "mlops", "ai engineer"]
        )
    ]
    blacklist_lower = [c.lower() for c in params.company_blacklist]
    excluded_lower = [kw.lower() for kw in params.keywords_excluded]

    jobs = []
    for comment in comments:
        text = comment.get("comment_text") or ""
        text_lower = text.lower()

        # Skip if no relevant keywords
        if not any(term in text_lower for term in search_terms):
            continue

        # Skip if excluded keywords found
        if any(kw in text_lower for kw in excluded_lower):
            continue

        # Must mention remote
        if "remote" not in text_lower:
            continue

        # Extract company name from first line (HN convention: "Company | Role | Location | ...")
        first_line = text.split("\n")[0].split("<")[0].strip()
        parts = [p.strip() for p in re.split(r"\s*[|]\s*", first_line)]
        company = parts[0] if parts else "Unknown"
        # Clean HTML tags from company name
        company = re.sub(r"<[^>]+>", "", company).strip()

        if company.lower() in blacklist_lower:
            continue

        # Try to find an apply URL in the comment
        urls = re.findall(r'href="(https?://[^"]+)"', text)
        if not urls:
            urls = re.findall(r"(https?://[^\s<\"']+)", text)
        apply_url = urls[0] if urls else ""

        # Clean HTML for description
        clean_text = re.sub(r"<[^>]+>", " ", text)
        clean_text = re.sub(r"\s+", " ", clean_text).strip()

        hn_url = f"https://news.ycombinator.com/item?id={comment.get('objectID', '')}"

        jobs.append(
            {
                "id": f"hn_{comment.get('objectID', '')}",
                "url": apply_url or hn_url,
                "listing_url": hn_url,
                "title": " | ".join(parts[1:3]) if len(parts) > 1 else params.title,
                "company": company,
                "description": _sanitize_description(clean_text[:5000]),
                "location": "Remote",
                "easy_apply": False,
                "apply_type": "external",
                "source": "hackernews",
            }
        )

    return jobs


# ── Biotech / Pharma career sites (Workday API) ─────────────────────────
_BIOTECH_WORKDAY_SITES = [
    # (display_name, tenant, wd_instance, site_path)
    ("Eli Lilly", "lilly", "wd5", "LLY"),
    ("Amgen", "amgen", "wd1", "Careers"),
    ("Pfizer", "pfizer", "wd1", "PfizerCareers"),
    ("BMS", "bristolmyerssquibb", "wd5", "BMS"),
    ("AstraZeneca", "astrazeneca", "wd3", "Careers"),
    ("Sanofi", "sanofi", "wd3", "SanofiCareers"),
    ("Roche", "roche", "wd3", "roche-ext"),
    ("Biogen", "biibhr", "wd3", "external"),
    ("Takeda", "takeda", "wd3", "External"),
]


def _workday_search(tenant: str, wd: str, site: str, query: str, limit: int = 20) -> List[Dict]:
    """Hit a Workday career site's public JSON API."""
    import urllib.request

    url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    payload = json.dumps(
        {
            "appliedFacets": {},
            "limit": limit,
            "offset": 0,
            "searchText": query,
        }
    ).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        log.warning(f"   Workday API error ({tenant}): {e}")
        return {}


def _workday_job_detail(tenant: str, wd: str, site: str, external_path: str) -> Dict:
    """Fetch full job description from Workday job detail endpoint."""
    import urllib.request

    url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{external_path}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}


def search_biotech(params: JobSearchParams) -> List[Dict]:
    """
    Search major biotech/pharma company career sites (Eli Lilly competitors)
    via their Workday public APIs. Filters for remote US roles only.
    """
    blacklist_lower = [c.lower() for c in params.company_blacklist]
    excluded_lower = [kw.lower() for kw in params.keywords_excluded]

    # Append "remote" to search query so Workday prioritises remote-eligible postings
    remote_query = f"{params.title} remote"

    all_jobs = []

    for display_name, tenant, wd, site in _BIOTECH_WORKDAY_SITES:
        if display_name.lower() in blacklist_lower:
            continue

        log.info(f"   🏥 Searching {display_name} careers...")

        data = _workday_search(tenant, wd, site, remote_query, limit=20)
        postings = data.get("jobPostings", [])
        total = data.get("total", 0)

        if not postings:
            continue

        log.info(f"      {display_name}: {total} results for '{params.title}'")

        for posting in postings:
            title = posting.get("title", "")
            location_text = posting.get("locationsText", "")
            external_path = posting.get("externalPath", "")
            posted_on = posting.get("postedOn", "")

            if not title or not external_path:
                continue

            # Quick pre-filter: skip obviously non-US/non-remote locations
            loc_lower = location_text.lower()
            non_us_only = [
                "india",
                "hyderabad",
                "china",
                "shanghai",
                "portugal",
                "dublin",
                "bogota",
                "barcelona",
                "singapore",
                "japan",
                "tokyo",
                "germany",
                "france",
                "paris",
                "london",
                "brazil",
                "mexico",
                "australia",
                "canada",
                "buenos aires",
                "seoul",
                "taiwan",
                "hong kong",
                "philippines",
                "vietnam",
                "zurich",
                "basel",
                "copenhagen",
                "amsterdam",
            ]
            # Only skip if location is EXCLUSIVELY non-US (not "2 Locations" etc)
            if any(x in loc_lower for x in non_us_only) and "location" not in loc_lower:
                continue

            # Title exclusions
            title_lower = title.lower()
            if any(kw in title_lower for kw in excluded_lower):
                continue

            # Age filter (Workday gives "Posted X Days Ago" or "Posted Today")
            if params.max_age_days and posted_on:
                if "30+" in posted_on:
                    continue
                days_match = re.search(r"(\d+)\s*Days?\s*Ago", posted_on, re.IGNORECASE)
                if days_match:
                    age = int(days_match.group(1))
                    if age > params.max_age_days:
                        continue

            base_url = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"
            apply_url = f"{base_url}{external_path}"

            # Fetch full description and check remoteType in detail
            detail = _workday_job_detail(tenant, wd, site, external_path)
            description = ""
            is_remote = False
            detail_location = location_text

            if detail:
                info = detail.get("jobPostingInfo", {})
                description = info.get("jobDescription", "")
                # Clean HTML tags
                description = re.sub(r"<[^>]+>", " ", description)
                description = re.sub(r"\s+", " ", description).strip()

                remote_type = (info.get("remoteType") or "").lower()
                detail_location = info.get("location", location_text)
                country = info.get("country", {})
                country_name = (
                    country.get("descriptor", "") if isinstance(country, dict) else str(country)
                )

                is_remote = remote_type == "remote"
                is_hybrid = "hybrid" in remote_type
                is_us = "united states" in country_name.lower()

                # Lilly: allow hybrid (user is in Indianapolis)
                # Everyone else: remote only
                lilly_exception = display_name == "Eli Lilly" and is_hybrid
                if not is_remote and not lilly_exception:
                    continue
                if country_name and not is_us:
                    continue

            job_id = (
                posting.get("bulletFields", [""])[0]
                or hashlib.sha256(apply_url.encode()).hexdigest()[:12]
            )

            all_jobs.append(
                {
                    "id": f"bio_{tenant}_{job_id}",
                    "url": apply_url,
                    "listing_url": apply_url,
                    "title": title,
                    "company": display_name,
                    "description": _sanitize_description(description[:5000]),
                    "location": detail_location,
                    "easy_apply": False,
                    "apply_type": "external",
                    "source": "biotech",
                }
            )

        # Small delay between companies to be polite
        time.sleep(random.uniform(0.5, 1.5))

    return all_jobs


def search_linkedin(params: JobSearchParams, proxy: Optional[str] = None) -> List[Dict]:
    """
    Search LinkedIn for jobs matching params (Easy Apply and external).
    Fetches full job descriptions for AI scoring.
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        raise RuntimeError("Run: pip install playwright && playwright install chromium") from None

    from urllib.parse import urlencode

    query_parts = {"keywords": params.title, "refresh": "true"}
    if params.location:
        query_parts["location"] = params.location
    if params.remote:
        query_parts["f_WT"] = "2"
    if params.max_age_days:
        query_parts["f_TPR"] = f"r{params.max_age_days * 86400}"

    url = f"https://www.linkedin.com/jobs/search/?{urlencode(query_parts)}"

    jobs = []
    with _stealth_playwright() as p:
        browser, context, page, owns_browser = _playwright_context(p, proxy)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            _ensure_logged_in(page, url)

            # Dismiss cookie consent and sign-in overlays that block job cards
            _dismiss_linkedin_overlays(page)

            # Wait for job cards: authenticated view uses job-card-container,
            # public/guest view uses job-search-card (base-search-card)
            try:
                page.wait_for_selector("div.job-card-container, div.job-search-card", timeout=12000)
            except Exception:
                raise RuntimeError(
                    f"No results found — LinkedIn may have changed layout. URL: {page.url}"
                ) from None

            page.evaluate("window.scrollTo(0, 500)")
            page.wait_for_timeout(2000)

            # Re-check after scroll — LinkedIn sometimes redirects after a delay
            _ensure_logged_in(page, url)

            jobs = _parse_job_cards(page)

            # Apply filters before fetching descriptions (no point fetching blacklisted jobs)
            if params.company_blacklist:
                bl = [c.lower() for c in params.company_blacklist]
                jobs = [j for j in jobs if j["company"].lower() not in bl]
            if params.keywords_excluded:
                excl = [kw.lower() for kw in params.keywords_excluded]
                jobs = [j for j in jobs if not any(kw in j["title"].lower() for kw in excl)]

            # Fetch full descriptions for remaining jobs
            if jobs:
                log.info(f"   Fetching descriptions for {len(jobs)} jobs...")
                for job in jobs:
                    if job["url"]:
                        job["description"], job["posted_ago"] = _fetch_description(
                            context, job["url"]
                        )

            if owns_browser:
                _save_session(context)
        finally:
            page.close()
            if owns_browser:
                browser.close()

    return jobs


def _extract_results_count(page) -> Optional[int]:
    """Extract the total results count from a LinkedIn job search page."""
    # fmt: off
    text = page.evaluate(
        "() => {"
        "  const selectors = ["
        "    '.jobs-search-results-list__subtitle',"
        "    '.jobs-search-results-list__title-heading small',"
        "    'header .jobs-search-results-list__text',"
        "    '.jobs-search-no-results-banner',"
        "  ];"
        "  for (const sel of selectors) {"
        "    const el = document.querySelector(sel);"
        "    if (el && el.innerText) return el.innerText.trim();"
        "  }"
        "  return '';"
        "}"
    )
    # fmt: on
    if not text:
        return None
    # Parse "1,234 results" or "4,000+" → integer
    match = re.search(r"([\d,]+)", text)
    if match:
        return int(match.group(1).replace(",", ""))
    return None


def market_snapshot(
    titles: List[str],
    location: Optional[str] = None,
    remote: bool = True,
    proxy: Optional[str] = None,
) -> List[Dict]:
    """
    Lightweight market scan: for each job title, query LinkedIn three times
    (all results, past week, past 24 hours) and record the result
    counts — no job cards parsed, no descriptions fetched.

    Returns list of snapshot entries saved to search_log.json.
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        raise RuntimeError("Run: pip install playwright && playwright install chromium") from None

    from urllib.parse import urlencode

    snapshots = []
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    def _snapshot_one_title(pg, title_kw, base_params):
        """Fetch counts for one title, returning (page, total, week, day).

        If the CDP page is evicted mid-operation, open a fresh page and
        retry once so a single flaky page doesn't abort the whole scan.
        """
        from playwright._impl._errors import TargetClosedError

        for attempt in range(2):
            try:
                # --- All-time count ---
                url_all = f"https://www.linkedin.com/jobs/search/?{urlencode(base_params)}"
                pg.goto(url_all, wait_until="domcontentloaded", timeout=30000)
                _ensure_logged_in(pg, url_all)
                pg.wait_for_timeout(3000)
                total_count = _extract_results_count(pg)
                log.info(f"   Total results: {total_count or 'unknown'}")

                # --- Past 1 week count ---
                week_params = {**base_params, "f_TPR": "r604800"}
                url_week = f"https://www.linkedin.com/jobs/search/?{urlencode(week_params)}"
                pg.goto(url_week, wait_until="domcontentloaded", timeout=30000)
                _ensure_logged_in(pg, url_week)
                pg.wait_for_timeout(3000)
                week_count = _extract_results_count(pg)
                log.info(f"   Past week:     {week_count or 'unknown'}")

                # --- Past 24 hours count ---
                day_params = {**base_params, "f_TPR": "r86400"}
                url_day = f"https://www.linkedin.com/jobs/search/?{urlencode(day_params)}"
                pg.goto(url_day, wait_until="domcontentloaded", timeout=30000)
                _ensure_logged_in(pg, url_day)
                pg.wait_for_timeout(3000)
                day_count = _extract_results_count(pg)
                log.info(f"   Past 24 hours: {day_count or 'unknown'}")

                return pg, total_count, week_count, day_count
            except TargetClosedError:
                if attempt == 0:
                    log.warning("   Page closed by browser, opening fresh page and retrying...")
                    pg = context.new_page()
                else:
                    log.error("   Page closed again on retry, skipping '%s'", title_kw)
                    return pg, None, None, None
        return pg, None, None, None  # unreachable

    with _stealth_playwright() as p:
        browser, context, page, owns_browser = _playwright_context(p, proxy)
        try:
            for i, title in enumerate(titles):
                if i > 0:
                    delay = 5 + i
                    log.info(f"⏳ Waiting {delay}s...")
                    time.sleep(delay)

                log.info(f"📊 Market snapshot: '{title}'")

                base_params = {"keywords": title, "refresh": "true"}
                if location:
                    base_params["location"] = location
                if remote:
                    base_params["f_WT"] = "2"

                page, total_count, week_count, day_count = _snapshot_one_title(
                    page, title, base_params
                )

                snapshots.append(
                    {
                        "search_title": title,
                        "source": "linkedin",
                        "total_results": total_count,
                        "past_week_results": week_count,
                        "past_day_results": day_count,
                        "location": location or "",
                        "remote": remote,
                        "timestamp": now,
                    }
                )

            if owns_browser:
                _save_session(context)
        finally:
            page.close()
            if owns_browser:
                browser.close()

    # Save to search log
    for snap in snapshots:
        save_search_log(snap)

    log.info(f"\n💾 Market data saved: {SEARCH_LOG_FILE}")
    return snapshots


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


def _extract_hiring_manager(page) -> Optional[Dict[str, str]]:
    """
    Extract the job poster / hiring manager info from a LinkedIn job detail page.

    Returns a dict with keys: name, headline, profile_url, compose_url
    or None if no hiring team section is found.
    """
    return page.evaluate(
        """() => {
        const body = document.body ? document.body.innerText : '';
        if (!body.includes('Meet the hiring team') && !body.includes('Job poster'))
            return null;

        const msgLink = document.querySelector(
            'a[href*="messaging/compose"][href*="JOB_DETAILS"]'
        );
        if (!msgLink) return null;

        let card = msgLink;
        for (let i = 0; i < 10 && card; i++) {
            const text = card.innerText || '';
            if (text.includes('Job poster') || text.includes('Meet the hiring team'))
                break;
            card = card.parentElement;
        }
        if (!card) return null;

        const profileLink = card.querySelector('a[href*="/in/"]');
        const cardText = card.innerText;
        const lines = cardText.split('\\n')
            .map(function(l) { return l.trim(); })
            .filter(function(l) { return l.length > 0; });

        var name = '';
        var headline = '';
        for (var i = 0; i < lines.length; i++) {
            var l = lines[i];
            if (l.charAt(0) === '\\u2022' || l === 'Job poster'
                || l === 'Message' || l === 'Connect'
                || l.includes('Meet the hiring team')) continue;
            if (!name) { name = l; continue; }
            if (!headline && l !== name) { headline = l; break; }
        }

        return {
            name: name,
            headline: headline,
            profile_url: profileLink ? profileLink.href.split('?')[0] : null,
            compose_url: msgLink.href,
        };
    }"""
    )


def _ai_draft_hiring_message(
    job: Dict, profile: "ApplicantProfile", poster_name: str
) -> Optional[str]:
    """Draft a short personalized message to the hiring manager using Claude."""
    if not _AI_AVAILABLE:
        return None
    try:
        client = _get_ai_client()
        description = _sanitize_description(job.get("description", ""))[:2000]
        prompt = f"""Write a short LinkedIn message (3 sentences) from a job applicant to the hiring manager / job poster.

Applicant: {profile.full_name}, {profile.current_title or "engineer"} with {profile.years_experience or "several"} years of experience.
Key skills: {", ".join(profile.specializations[:4]) if profile.specializations else "infrastructure engineering"}

Job: {job.get("title")} at {job.get("company")}
Poster name: {poster_name}
Job description snippet: {description[:800]}

Guidelines:
- Start with "Hey <first name>," — that's the only greeting
- Sentence 1: Say you applied for the role (use the exact title)
- Sentence 2: ONE specific detail — a tool overlap, a similar problem you solved, or a concrete fact from your background that connects to the job. Not a summary of your whole career.
- Sentence 3: A brief, natural closer — a question about the team/stack, or saying you'd love to chat. Keep it casual.
- Do NOT start sentences with "I've spent the last N years". Do NOT list multiple skills. Pick ONE thing.
- 3 sentences. Under 250 characters after the greeting.
- Sound like a person dashing off a quick note, not crafting a pitch.
- NEVER use: "excited", "passionate", "eager", "thrilled", "aligns", "resonates", "looking forward", "opportunity", "I believe", "sounds like", "exactly what", "wheelhouse", "culture", "I'd love to bring"
- NEVER use em dashes (—) or double dashes (--). Use commas, periods, or rewrite the sentence instead.
- NO subject line, NO sign-off"""

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        stats.add_ai_tokens(response.usage)
        msg = response.content[0].text.strip()
        # Strip em dashes — they scream "AI-written"
        msg = msg.replace(" — ", ", ").replace(" -- ", ", ").replace("—", ", ").replace("--", ", ")
        # Strip any AI preamble, self-corrections, or extra lines
        lines = msg.split("\n")
        clean = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("*") or stripped.startswith("---"):
                break  # AI started self-correcting
            if stripped:
                clean.append(stripped)
        msg = " ".join(clean) if clean else msg
        # Strip stale greeting prefixes
        for prefix in ("Subject:", "Hi,", "Hello,"):
            if msg.startswith(prefix):
                msg = msg[len(prefix) :].strip()
        return msg
    except Exception as e:
        log.warning(f"   AI hiring message draft failed: {e}")
        return None


def _send_hiring_manager_message(  # noqa: C901
    page,
    compose_url: str,
    message: str,
    poster_name: str,
    job_title: str = "",
    company: str = "",
) -> str:
    """
    Navigate to the LinkedIn messaging compose URL and send a message.
    Returns 'sent', 'failed', or an error description.
    """
    try:
        page.goto(compose_url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3000)

        # Find the message textbox
        textbox = page.query_selector('div.msg-form__contenteditable[role="textbox"]')
        if not textbox:
            # Fallback: any contenteditable with message-related label
            textbox = page.query_selector('[role="textbox"][aria-label*="message" i]')
        if not textbox:
            log.warning("   Could not find message compose textbox")
            return "failed: no textbox found"

        # Check for subject line (InMail) and fill if present
        subject_input = page.query_selector('input.msg-form__subject, input[name="subject"]')
        if subject_input:
            subject_input.click()
            subj = f"Re: {job_title}" if job_title else "Re: your job posting"
            subject_input.fill(subj)
            page.wait_for_timeout(300)

        # Type the message
        textbox.click()
        page.wait_for_timeout(300)
        textbox.type(message, delay=20)
        page.wait_for_timeout(500)

        # Find and click the send button
        send_btn = page.query_selector("button.msg-form__send-btn")
        if not send_btn:
            send_btn = page.query_selector('button[type="submit"]:has(svg)')
        if not send_btn:
            log.warning("   Could not find send button")
            return "failed: no send button"

        send_btn.click()
        page.wait_for_timeout(2000)

        log.info(f"   ✉️  Message sent to {poster_name}")
        return "sent"
    except Exception as exc:
        log.warning(f"   Message send failed: {exc}")
        return f"failed: {exc}"


def _message_hiring_manager_after_apply(
    job: Dict,
    profile: "ApplicantProfile",
    proxy: Optional[str] = None,
) -> tuple:
    """
    After a successful application, check the job page for a hiring manager
    and send a short personalized message.
    Returns (status, message_text, poster_name) or (None, None, None) if skipped.
    """
    if not profile.message_hiring_manager:
        return None, None, None

    try:
        import playwright  # noqa: F401
    except ImportError:
        return None, None, None

    with _stealth_playwright() as p:
        browser, context, page, owns_browser = _playwright_context(p, proxy)
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3000)

            poster = _extract_hiring_manager(page)
            if not poster or not poster.get("compose_url"):
                log.info("   ℹ️  No hiring manager found for %s", job.get("title"))
                return None, None, None

            log.info(f"   👤 Found hiring manager: {poster['name']} — {poster.get('headline', '')}")

            message = _ai_draft_hiring_message(job, profile, poster["name"])
            if not message:
                log.warning("   Could not draft message — skipping")
                return None, None, None

            log.info(f"   💬 Sending: {message[:80]}...")
            status = _send_hiring_manager_message(
                page,
                poster["compose_url"],
                message,
                poster["name"],
                job_title=job.get("title", ""),
                company=job.get("company", ""),
            )
            return status, message, poster["name"]
        finally:
            page.close()
            if owns_browser:
                browser.close()


def _dismiss_save_dialog(page) -> None:
    """Dismiss the 'Save this application?' dialog if it appeared."""
    try:
        discard_btn = page.query_selector(
            "button:has-text('Discard'), button[data-test-dialog-secondary-btn]"
        )
        if discard_btn and discard_btn.is_visible():
            discard_btn.click()
            page.wait_for_timeout(1000)
            log.debug("Dismissed 'Save this application?' dialog")
    except Exception as exc:
        log.debug("Save dialog dismiss failed (non-critical): %s", exc)


def _get_modal_text(page) -> str:
    """Get the text content of the Easy Apply modal for stall detection."""
    modal = page.query_selector(".artdeco-modal, .jobs-easy-apply-modal, [role='dialog']")
    if modal:
        try:
            return modal.inner_text()[:800]
        except Exception:  # noqa: S110
            log.debug("Failed to read modal text for stall detection")
    return ""


def _navigate_form(page, profile, owns_browser, context, job_id: str = "") -> str:  # noqa: C901
    """Navigate through multi-step Easy Apply form. Returns status string."""
    max_steps = 15
    stalled = 0
    for _step in range(max_steps):
        page.wait_for_timeout(random.randint(800, 2200))

        # Dismiss "Save this application?" dialog if it appeared
        _dismiss_save_dialog(page)

        # Check if modal is still open
        modal = page.query_selector(".artdeco-modal, .jobs-easy-apply-modal, [role='dialog']")
        if not modal:
            _dump_form_debug(page, job_id, "Application modal closed unexpectedly")
            return "failed: lost track of form steps"

        # Fill screening questions on EVERY page (new fields appear after each Next)
        _answer_screening_questions(page, profile)

        submit_btn = page.query_selector(
            "button[aria-label='Submit application'], button[aria-label*='Submit']"
        )
        review_btn = page.query_selector(
            "button[aria-label='Review your application'], button[aria-label*='Review']"
        )
        next_btn = page.query_selector(
            "button[aria-label='Continue to next step'], button[aria-label*='Continue']"
        )

        if submit_btn:
            _dismiss_all_typeaheads(page)
            _safe_click(submit_btn, page)
            # Wait for confirmation with retries — LinkedIn can be slow to render
            success = None
            confirmation_sel = (
                "[aria-label='Your application was sent'], "
                ".artdeco-modal__header:has-text('Application submitted'), "
                "h2:has-text('application was sent'), "
                ".artdeco-inline-feedback--success, "
                "[data-test-modal-id='post-apply-modal']"
            )
            try:
                success = page.wait_for_selector(confirmation_sel, timeout=8000)
            except Exception:
                # Final fallback: check if form validation errors appeared (means it didn't submit)
                validation_err = page.query_selector(
                    ".artdeco-inline-feedback--error, "
                    "[data-test-form-element-error], "
                    ".fb-dash-form-element__error-text"
                )
                if validation_err:
                    err_text = validation_err.inner_text()[:200]
                    if owns_browser:
                        _save_session(context)
                    _dump_form_debug(
                        page, job_id, f"Submit clicked but validation errors: {err_text}"
                    )
                    return f"failed: form validation errors after submit: {err_text}"
                # No confirmation AND no errors — check once more
                success = page.query_selector(confirmation_sel)
            if owns_browser:
                _save_session(context)
            return "submitted" if success else "submitted (unconfirmed — check LinkedIn)"
        elif review_btn:
            _dismiss_all_typeaheads(page)
            cur_text = _get_modal_text(page)
            _safe_click(review_btn, page)
            page.wait_for_timeout(1500)
            _dismiss_save_dialog(page)
            new_text = _get_modal_text(page)
            if new_text == cur_text:
                # Stalled — check for validation errors and try to fix them
                errors = _get_validation_errors(page)
                filled = _fill_empty_required_fields(page, profile)
                if filled > 0:
                    log.debug("Review stalled: filled %d fields, retrying", filled)
                    stalled = 0  # reset — we made progress
                else:
                    stalled += 1
                    err_hint = f" errors={errors}" if errors else ""
                    log.debug("Review click didn't change modal (stalled=%d)%s", stalled, err_hint)
                    if stalled >= 3:
                        reason = f"Review not advancing (step {_step + 1}/{max_steps})"
                        if errors:
                            reason += f" (validation: {'; '.join(errors)[:200]})"
                        _dump_form_debug(page, job_id, reason)
                        return f"failed: form stuck — {reason}"
            else:
                stalled = 0
        elif next_btn:
            _dismiss_all_typeaheads(page)
            cur_text = _get_modal_text(page)
            _safe_click(next_btn, page)
            page.wait_for_timeout(1500)
            _dismiss_save_dialog(page)
            new_text = _get_modal_text(page)
            if new_text == cur_text:
                # Stalled — check for validation errors and try to fix them
                errors = _get_validation_errors(page)
                filled = _fill_empty_required_fields(page, profile)
                if filled > 0:
                    log.debug("Next stalled: filled %d fields, retrying", filled)
                    stalled = 0  # reset — we made progress
                else:
                    stalled += 1
                    err_hint = f" errors={errors}" if errors else ""
                    log.debug("Next click didn't change modal (stalled=%d)%s", stalled, err_hint)
                    if stalled >= 3:
                        reason = f"Next not advancing (step {_step + 1}/{max_steps})"
                        if errors:
                            reason += f" (validation: {'; '.join(errors)[:200]})"
                        _dump_form_debug(page, job_id, reason)
                        return f"failed: form stuck — {reason}"
            else:
                stalled = 0
        else:
            stalled += 1
            if stalled >= 3:
                _dump_form_debug(page, job_id, "No Next/Submit/Review buttons found")
                return "failed: lost track of form steps"
            page.wait_for_timeout(1500)

    _dump_form_debug(page, job_id, "Exceeded max form steps")
    return "failed: exceeded max form steps (15)"


def submit_easy_apply(job: Dict, profile: ApplicantProfile, proxy: Optional[str] = None) -> str:
    """
    Submit a LinkedIn Easy Apply application.
    Returns 'submitted' on success, 'failed: <reason>' on failure.
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

    with _stealth_playwright() as p:
        browser, context, page, owns_browser = _playwright_context(p, proxy)
        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)

            easy_apply_sel = "[aria-label*='Easy Apply'], button:has-text('Easy Apply'), a:has-text('Easy Apply')"
            easy_apply_btn = None
            for _wait in range(4):
                easy_apply_btn = page.query_selector(easy_apply_sel)
                if easy_apply_btn:
                    break
                page.wait_for_timeout(2000)
            if not easy_apply_btn:
                return "failed: Easy Apply button not found — job may no longer be active"
            _safe_click(easy_apply_btn, page)
            page.wait_for_timeout(1500)

            phone_field = page.query_selector("input[name='phoneNumber']")
            if phone_field:
                phone_field.fill(profile.phone)

            resume_path = str(Path(profile.resume_path).expanduser())
            resume_input = page.query_selector(
                "input[type='file'][name*='resume'], input[type='file'][accept*='pdf']"
            )
            if resume_input:
                resume_input.set_input_files(resume_path)

            _answer_screening_questions(page, profile)
            return _navigate_form(page, profile, owns_browser, context, job_id=job.get("id", ""))

        except ApplicationAbortError as e:
            log.warning(f"   🛡️  Application aborted: {e}")
            return f"aborted: {e}"
        except Exception as e:
            return f"failed: {e}"
        finally:
            page.close()
            if owns_browser:
                browser.close()


# ---------------------------------------------------------------------------
# External ATS form filler (Workday, Greenhouse, Lever, iCIMS, etc.)
# ---------------------------------------------------------------------------


_MAX_EXTERNAL_STEPS = 20


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Retry queue — re-queue failures for Q2 (MCP Playwright) or Q3 (dashboard)
# ---------------------------------------------------------------------------

# Failure categories eligible for retry via Q2/Q3
_DEEP_APPLY_ELIGIBLE_CATEGORIES = frozenset(
    {
        "form_stuck",
        "validation_error",
        "captcha",
        "no_apply_button",
        "login_wall",
        "modal_lost",
        "max_steps",
        "timeout",
        "unknown_error",
    }
)


def _load_deep_apply_queue() -> List[Dict]:
    if DEEP_APPLY_QUEUE_FILE.exists():
        try:
            return json.loads(DEEP_APPLY_QUEUE_FILE.read_text())
        except Exception:
            return []
    return []


def _save_deep_apply_queue(queue: List[Dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEEP_APPLY_QUEUE_FILE.write_text(json.dumps(queue, indent=2))


def _deep_apply_eligible(entry: Dict, already_queued_ids: set) -> bool:
    """Check if a failed application is eligible for Q2 retry."""
    if not entry.get("status", "").startswith("failed"):
        return False
    if (entry.get("match_score") or 0) < 0.7:
        return False
    if entry.get("failure_category") not in _DEEP_APPLY_ELIGIBLE_CATEGORIES:
        return False
    if entry.get("job_id") in already_queued_ids:
        return False
    return True


def _queue_for_deep_apply(app_entry: Dict, queue_tier: str = "q2") -> None:
    """Queue a failed application for Q2 (assisted) or Q3 (dashboard) retry."""
    queue = _load_deep_apply_queue()
    queue.append(
        {
            "job_id": app_entry["job_id"],
            "title": app_entry.get("title", ""),
            "company": app_entry.get("company", ""),
            "url": app_entry.get("url", ""),
            "ats_url": app_entry.get("ats_url", ""),
            "match_score": app_entry.get("match_score", 0),
            "failure_reason": app_entry.get("failure_category", ""),
            "original_status": app_entry.get("status", ""),
            "queued_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pre_computed": {
                "cover_letter_path": app_entry.get("cover_letter_path", ""),
                "field_answers": list(stats._field_fills),
                "scoring_reasoning": app_entry.get("reasoning", ""),
            },
            "status": "pending",
            "queue": queue_tier,
            "q2_attempts": 0,
            "decision_log": [],
            "deep_apply_status": None,
            "deep_apply_timestamp": None,
            "deep_apply_cost": None,
        }
    )
    _save_deep_apply_queue(queue)
    tier_label = "Q2 assisted" if queue_tier == "q2" else "Q3 dashboard"
    log.info(
        "   \U0001f4cb Queued for %s: %s - %s (score: %.2f)",
        tier_label,
        app_entry.get("company", "?"),
        app_entry.get("title", "?"),
        app_entry.get("match_score", 0),
    )


def _generate_deep_apply_prompt(queue_entry: Dict, profile: ApplicantProfile) -> str:
    """Generate a structured prompt for the Claude Chrome extension to complete an application."""
    pre = queue_entry.get("pre_computed", {})
    pct = int(queue_entry.get("match_score", 0) * 100)

    # Build field answers section (deduplicated)
    field_lines = []
    seen_fields: set = set()
    for fa in pre.get("field_answers", []):
        key = f"{fa['field'].lower().strip()}: {fa['value'].strip()}"
        if key in seen_fields:
            continue
        seen_fields.add(key)
        field_lines.append(f"- {fa['field']}: {fa['value']}")
    field_section = "\n".join(field_lines) if field_lines else "(no pre-filled answers available)"

    # Build screening answers section -- skip keys already covered in
    # field_answers above, and deduplicate exact key:value pairs
    screening_lines = []
    for k, v in profile.screening_answers.items():
        norm = f"{k.lower().strip()}: {v.strip().lower()}"
        if norm in seen_fields:
            continue
        seen_fields.add(norm)
        screening_lines.append(f"- {k}: {v}")
    screening_section = "\n".join(screening_lines) if screening_lines else "(none)"

    # Cover letter: inline the content so it can be pasted into Claude Desktop
    cover_text = pre.get("cover_letter_text", "")
    if not cover_text:
        cl_path = pre.get("cover_letter_path", "")
        if cl_path:
            try:
                cover_text = Path(cl_path).read_text().strip()
            except Exception:
                pass
    if cover_text:
        cover_section = (
            "If a cover letter field appears, paste the following cover letter:\n\n"
            f"---\n{cover_text}\n---"
        )
    else:
        cover_section = "No cover letter was generated for this application."

    # Resume filename only (user has it in ~/Downloads on their workstation)
    resume_name = Path(profile.resume_path).name if profile.resume_path else "resume.pdf"

    return f"""I need you to complete a job application. Follow these steps:

## Job Details
- Position: {queue_entry.get("title", "Unknown")} at {queue_entry.get("company", "Unknown")}
- Application URL: {queue_entry.get("url", "")}
- Match Score: {pct}%

## Step 1: Navigate
Open the application URL above.

## Step 2: Account/Login
If you see a login wall or account creation requirement:
- Create an account using: {profile.email}
- Generate a secure password
- If email verification is needed, open Gmail in a new tab, find the verification email, get the code, return and enter it

## Step 3: Fill the Application
Use these answers for form fields:

{field_section}

Additional screening answers (from profile):
{screening_section}

## Step 4: Resume
Upload my resume from: ~/Downloads/{resume_name}

## Step 5: Cover Letter
{cover_section}

## Step 6: Submit
Check any consent/terms checkboxes, then click Submit.

## If you encounter anything not covered above
Use your judgment to complete the application. Key facts:
- Authorized to work in US: Yes
- Requires sponsorship: No
- Willing to relocate: No
- Open to remote: Yes
- Years of experience: {profile.years_experience or 12}
- Current employer: {profile.current_employer or "N/A"}
- Current title: {profile.current_title or "N/A"}"""


def _mark_deep_apply_done(
    job_id: str,
    status: str,
    reason: Optional[str],
    decision_log: Optional[List[Dict]] = None,
) -> bool:
    """Mark a queue entry as done and update the application log."""
    queue = _load_deep_apply_queue()
    found = False
    for entry in queue:
        if entry["job_id"] == job_id:
            entry["status"] = "done"
            entry["deep_apply_status"] = status
            entry["deep_apply_timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if decision_log:
                entry["decision_log"] = decision_log
            found = True
            break
    if not found:
        return False
    _save_deep_apply_queue(queue)

    # Update the original application log entry
    all_apps = load_log() if LOG_FILE.exists() else []
    for app in all_apps:
        if app.get("job_id") == job_id:
            app["deep_apply_status"] = status
            app["deep_apply_timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if reason:
                app["deep_apply_reason"] = reason
            break
    if all_apps:
        LOG_FILE.write_text(json.dumps(all_apps, indent=2))

    return True


def _escalate_to_q3(job_id: str, reason: str) -> bool:
    """Escalate a Q2 entry to Q3 (dashboard) after assisted apply failure."""
    queue = _load_deep_apply_queue()
    for entry in queue:
        if entry["job_id"] == job_id:
            entry["queue"] = "q3"
            entry["status"] = "pending"
            entry["q2_attempts"] = entry.get("q2_attempts", 0)
            entry["escalation_reason"] = reason
            _save_deep_apply_queue(queue)
            log.info(
                "   Escalated to Q3 (dashboard): %s - %s (%s)",
                entry.get("company", "?"),
                entry.get("title", "?"),
                reason,
            )
            return True
    return False


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


def load_log() -> List[Dict]:
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text())
        except Exception:
            return []
    return []


def save_log(entries: List[Dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = load_log()
    existing.extend(entries)
    LOG_FILE.write_text(json.dumps(existing, indent=2))
    log.info(f"\n💾 Log updated: {LOG_FILE}")


def save_search_log(entry: Dict):
    """Append a search-result entry to search_log.json."""
    import fcntl

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = open(SEARCH_LOG_FILE, "a+")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        fd.seek(0)
        raw = fd.read()
        existing: List[Dict] = json.loads(raw) if raw.strip() else []
        existing.append(entry)
        fd.seek(0)
        fd.truncate()
        fd.write(json.dumps(existing, indent=2))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def already_applied(log_entries: List[Dict]) -> set:
    """Return a set of canonical URLs and job IDs for previously attempted applications.

    Includes both submitted and failed entries so we don't re-attempt the same
    job with a different tracking-parameter URL.
    """
    result = set()
    for e in log_entries:
        status = e.get("status", "")
        if (
            status.startswith("submitted")
            or status.startswith("failed")
            or status.startswith("skipped")
        ):
            if e.get("url"):
                # Strip tracking params for canonical matching
                canonical = re.sub(r"\?.*$", "", e["url"])
                result.add(canonical)
                result.add(e["url"])
            if e.get("job_id"):
                result.add(e["job_id"])
    return result


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
