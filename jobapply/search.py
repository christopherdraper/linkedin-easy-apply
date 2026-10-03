"""Job search sources (LinkedIn, RemoteOK, HackerNews, biotech Workday sites)
and the LinkedIn market snapshot scan."""

import hashlib
import html
import json
import logging
import random
import re
import time
from typing import Callable, Dict, List, Optional

from jobapply.applog import save_search_log
from jobapply.browser import (
    LinkedInBlockedError,
    _assert_linkedin_not_blocked,
    _dismiss_linkedin_overlays,
    _ensure_logged_in,
    _playwright_context,
    _save_session,
    _stealth_playwright,
)
from jobapply.config import SEARCH_LOG_FILE
from jobapply.profile import JobSearchParams
from jobapply.safety import _sanitize_description

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
        _assert_linkedin_not_blocked(desc_page)

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
    except LinkedInBlockedError:
        raise
    except Exception as exc:
        log.debug("Description fetch failed for %s: %s", url, exc)
    finally:
        desc_page.close()
    return "", ""


def _card_title(title_el) -> str:
    """The visible job title from a logged-in LinkedIn card's title link.

    The link holds the title twice: once in <strong> for display and once in a
    visually-hidden span for screen readers, which verified postings suffix
    with " with verification". inner_text() returns both on separate lines.
    """
    strong = title_el.query_selector("strong")
    text = (strong or title_el).inner_text().strip()
    first_line = text.splitlines()[0].strip() if text else ""
    return re.sub(r"\s+with verification$", "", first_line)


# LinkedIn's 2026 "AI search" results page (/jobs/search-results/) has no
# stable class names and no links on its cards: each card is a clickable block
# carrying componentkey="job-card-component-ref-<jobId>" (nested twice).
_AI_SEARCH_CARD_SELECTOR = "[componentkey^='job-card-component-ref-']"
_AI_SEARCH_CARDS_JS = """els => {
  const seen = new Set(), out = [];
  for (const e of els) {
    const key = e.getAttribute('componentkey');
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({
      jobId: key.replace('job-card-component-ref-', ''),
      paras: [...e.querySelectorAll('p')].map(p => p.innerText.trim()).filter(Boolean),
    });
  }
  return out;
}"""


def _ai_search_job(job_id: str, paras: List[str]) -> Optional[Dict]:
    """One job dict from an AI-search card: title, company, location paragraphs."""
    if not job_id.isdigit() or len(paras) < 2:
        return None
    # The title paragraph repeats the title for screen readers, prefixed
    # "Selected, " on the open card; the last line is the visible one.
    title = paras[0].splitlines()[-1].strip()
    title = re.sub(r"\s*\(Verified job\)$", "", title)
    # Same canonical URL the classic cards produce, so job ids (a hash of it)
    # still match the applied log and the score cache.
    url = f"https://www.linkedin.com/jobs/view/{job_id}/"
    easy = any(p.strip().lower() == "easy apply" for p in paras)
    return {
        "id": f"li_{hashlib.sha256(url.encode()).hexdigest()[:12]}",
        "title": title,
        "company": paras[1],
        "location": paras[2] if len(paras) > 2 else "",
        "url": url,
        "description": "",
        "apply_type": "easy_apply" if easy else "external",
    }


def _parse_ai_search_cards(page) -> List[Dict]:
    """Extract jobs from LinkedIn's AI-search results layout."""
    cards = page.eval_on_selector_all(_AI_SEARCH_CARD_SELECTOR, _AI_SEARCH_CARDS_JS)
    jobs = [_ai_search_job(c["jobId"], c["paras"]) for c in cards[:25]]
    return [j for j in jobs if j]


def _parse_job_cards(page) -> List[Dict]:
    """Extract job data from visible job cards on the search results page.

    Supports both authenticated LinkedIn (div.job-card-container) and
    public/guest LinkedIn (div.job-search-card) page layouts.
    """
    try:
        cards = page.query_selector_all("div.job-card-container")
        is_public = False
        if not cards and page.query_selector(_AI_SEARCH_CARD_SELECTOR):
            return _parse_ai_search_cards(page)
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
                        "title": (
                            title_el.inner_text().strip() if is_public else _card_title(title_el)
                        ),
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
    ("Eli Lilly", "lilly", "wd115", "LLY"),  # moved from wd5, which now answers 422
    ("Amgen", "amgen", "wd1", "Careers"),
    ("Pfizer", "pfizer", "wd1", "PfizerCareers"),
    ("BMS", "bristolmyerssquibb", "wd5", "BMS"),
    ("AstraZeneca", "astrazeneca", "wd3", "Careers"),
    ("Sanofi", "sanofi", "wd3", "SanofiCareers"),
    ("Roche", "roche", "wd3", "roche-ext"),
    ("Biogen", "biibhr", "wd3", "external"),
    ("Takeda", "takeda", "wd3", "External"),
]


def _workday_search(tenant: str, wd: str, site: str, query: str, limit: int = 20) -> Dict:
    """Hit a Workday career site's public JSON API.

    Returns the parsed CXS response dict ({} on error); callers read
    data.get("jobPostings", []).
    """
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
                description = _html_to_text(info.get("jobDescription", ""))

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
                    "description": _sanitize_description(description)[:5000],
                    "location": detail_location,
                    "easy_apply": False,
                    "apply_type": "external",
                    "source": "biotech",
                }
            )

        # Small delay between companies to be polite
        time.sleep(random.uniform(0.5, 1.5))

    return all_jobs


def _html_to_text(markup: str) -> str:
    """A Workday job description as plain text, one paragraph or bullet per line.

    Lines matter: _sanitize_description drops whole lines that look like prompt
    injection. Descriptions used to be flattened to ONE line first, so a single
    phrase (GM's "system prompt", 2026-09-30) erased the entire description and
    the scorer judged the job on its title alone.
    """
    text = re.sub(r"(?i)<br\s*/?>|</(?:p|li|div|h[1-6]|tr|ul|ol)>", "\n", markup or "")
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def _posted_within(posted_on: str, max_age_days: Optional[int]) -> bool:
    """Workday's "Posted 3 Days Ago" / "Posted 30+ Days Ago" against a day limit."""
    if not max_age_days or not posted_on:
        return True
    if "30+" in posted_on:
        return False
    m = re.search(r"(\d+)\s*Days?\s*Ago", posted_on, re.IGNORECASE)
    return not m or int(m.group(1)) <= max_age_days


def search_workday_sites(params: JobSearchParams) -> List[Dict]:
    """Search employers' own Workday career sites (params.workday_sites).

    Built while LinkedIn had restricted the applicant's account (2026-09-30):
    it needs no LinkedIn session and uses only Workday's public job API. A
    posting is kept if it is in the US and either remote or at a location
    matching params.local_terms; whether that is a real commute is left to the
    scorer. The country check matters: Lilly writes India as "IN: Hyderabad".
    """
    blacklist_lower = [c.lower() for c in params.company_blacklist]
    excluded_lower = [kw.lower() for kw in params.keywords_excluded]
    local_lower = [t.lower() for t in params.local_terms]
    jobs: List[Dict] = []
    for site_cfg in params.workday_sites:
        name = site_cfg.get("name", "")
        tenant, wd, site = site_cfg.get("tenant"), site_cfg.get("wd"), site_cfg.get("site")
        if not (tenant and wd and site) or name.lower() in blacklist_lower:
            continue
        data = _workday_search(tenant, wd, site, params.title, limit=20)
        postings = data.get("jobPostings", [])
        log.info(f"   🏢 {name}: {data.get('total', 0)} results for '{params.title}'")
        for posting in postings:
            title = posting.get("title", "")
            path = posting.get("externalPath", "")
            if not title or not path or any(kw in title.lower() for kw in excluded_lower):
                continue
            if not _posted_within(posting.get("postedOn", ""), params.max_age_days):
                continue
            info = _workday_job_detail(tenant, wd, site, path).get("jobPostingInfo") or {}
            country = (info.get("country") or {}).get("descriptor", "")
            if "united states" not in country.lower():
                continue
            location = info.get("location") or posting.get("locationsText", "")
            others = " ".join(info.get("additionalLocations") or [])
            remote = (info.get("remoteType") or "").lower() == "remote"
            local = any(t in f"{location} {others}".lower() for t in local_lower)
            if not (remote or local):
                continue
            url = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{path}"
            description = _html_to_text(info.get("jobDescription", ""))
            jobs.append(
                {
                    "id": f"wd_{tenant}_{hashlib.sha256(url.encode()).hexdigest()[:12]}",
                    "url": url,
                    "listing_url": url,
                    "title": title,
                    "company": name,
                    "description": _sanitize_description(description)[:5000],
                    "location": "Remote" if remote and not local else location,
                    "posted_ago": posting.get("postedOn", ""),
                    "easy_apply": False,
                    "apply_type": "external",
                    "source": "workday",
                }
            )
        time.sleep(random.uniform(0.5, 1.5))
    return jobs


# Scroll LinkedIn's results pane one step and report how many job cards have
# rendered. The pane uses obfuscated class names, so it is located by computed
# overflow: the first scrollable ancestor of a job card.
_SCROLL_JOB_PANE_JS = """() => {
  const first = document.querySelector(
    "li.scaffold-layout__list-item, div.job-card-container, [componentkey^='job-card-component-ref-']");
  let n = first;
  while (n && n !== document.body) {
    const s = getComputedStyle(n);
    if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && n.scrollHeight > n.clientHeight + 10) {
      n.scrollBy(0, 600);
      break;
    }
    n = n.parentElement;
  }
  return document.querySelectorAll(
    "div.job-card-container, div.job-search-card, [componentkey^='job-card-component-ref-']").length;
}"""


def _hydrate_job_list(page, max_steps: int = 15, settle_ms: int = 700) -> int:
    """Scroll LinkedIn's results pane until every job card has rendered.

    The authenticated results list is virtualized: all ~25 slots exist on load,
    but only the first ~7 render a card until the list PANE (not the window)
    scrolls. Scrolling the window, as this used to, left ~72% of every results
    page unparsed. Stops once the rendered card count holds steady.
    """
    last, stable = -1, 0
    for _ in range(max_steps):
        count = page.evaluate(_SCROLL_JOB_PANE_JS)
        page.wait_for_timeout(settle_ms)
        if count == last:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last = count
    return max(last, 0)


def _assert_linkedin_session(context, page) -> None:
    """Raise "session expired" if LinkedIn is serving this search logged out.

    A revoked session does not redirect job search to the authwall: LinkedIn
    serves the PUBLIC results page, _parse_job_cards falls back to the guest
    card layout, and every application then dies on a sign-in wall. On
    2026-09-23 a batch ran ~an hour like that. Cookie and layout are checked
    independently so either signal alone stops it.

    Deliberately no auto-login here: logging a revoked session back in hits an
    SMS challenge, and retrying would text the applicant's phone on every run.
    """
    has_li_at = any(c.get("name") == "li_at" for c in context.cookies("https://www.linkedin.com"))
    guest_view = (
        page.query_selector("div.job-card-container") is None
        and page.query_selector("div.job-search-card") is not None
    )
    if not has_li_at or guest_view:
        raise RuntimeError(
            "LinkedIn session expired: search is being served logged out "
            f"(li_at={has_li_at}, guest_layout={guest_view}). Re-authenticate and retry."
        )


# Pause between LinkedIn job-page loads. On 2026-09-23, after 348df6f made every
# search read 25 results, ~25 unpaced page loads per search (repeated across
# overlapping-title reruns) preceded LinkedIn revoking the session.
_LINKEDIN_PAGE_GAP_S = (2.0, 5.0)


def _fetch_descriptions(
    context,
    page,
    jobs: List[Dict],
    need_description: Optional[Callable[[Dict], bool]] = None,
    gap_s: tuple = _LINKEDIN_PAGE_GAP_S,
) -> int:
    """Load job pages only for jobs that need a description, pacing the loads.

    Jobs whose outcome is already decided (applied, or cached as below the bar
    or deal-broken) keep an empty description: re-running overlapping titles
    used to reload every one of those pages. Returns the number of pages loaded.
    """
    for job in jobs:
        job.setdefault("posted_ago", "")
    todo = [j for j in jobs if j.get("url") and (need_description is None or need_description(j))]
    skipped = len(jobs) - len(todo)
    note = f" (skipping {skipped} already decided)" if skipped else ""
    log.info(f"   Fetching descriptions for {len(todo)} jobs{note}...")
    for i, job in enumerate(todo):
        if i:
            page.wait_for_timeout(int(random.uniform(*gap_s) * 1000))
        job["description"], job["posted_ago"] = _fetch_description(context, job["url"])
    return len(todo)


def search_linkedin(
    params: JobSearchParams,
    proxy: Optional[str] = None,
    need_description: Optional[Callable[[Dict], bool]] = None,
) -> List[Dict]:
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
                page.wait_for_selector(
                    f"div.job-card-container, div.job-search-card, {_AI_SEARCH_CARD_SELECTOR}",
                    timeout=12000,
                )
            except Exception:
                _assert_linkedin_not_blocked(page)
                raise RuntimeError(
                    f"No results found — LinkedIn may have changed layout. URL: {page.url}"
                ) from None

            page.evaluate("window.scrollTo(0, 500)")
            page.wait_for_timeout(2000)
            _hydrate_job_list(page)
            _assert_linkedin_session(context, page)

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

            # Fetch full descriptions, only where the batch will use them.
            if jobs:
                _fetch_descriptions(context, page, jobs, need_description)

            if owns_browser:
                _save_session(context)
        finally:
            page.close()
            if owns_browser:
                browser.close()

    return jobs


# Exact result counts from LinkedIn's own job-search API (the endpoint its
# jobs page calls). The 2026 AI-search results page shows only "99+ results",
# so page scraping can no longer read a count. Called from a linkedin.com page
# so the session cookies and CSRF token apply. Omitting a location searches
# the member's country, as a keyword-only jobs URL does.
_VOYAGER_JOB_COUNT_JS = """async ({keywords, remote, tpr}) => {
  const csrf = (document.cookie.match(/JSESSIONID="?([^";]+)/) || [])[1] || '';
  const kw = encodeURIComponent(keywords).replace(/\\(/g, '%28').replace(/\\)/g, '%29');
  const filters = [];
  if (remote) filters.push('workplaceType:List(2)');
  if (tpr) filters.push('timePostedRange:List(' + tpr + ')');
  const url = '/voyager/api/voyagerJobsDashJobCards'
    + '?decorationId=com.linkedin.voyager.dash.deco.jobs.search.JobSearchCardsCollection-220'
    + '&count=1&q=jobSearch&start=0&query=(origin:JOB_SEARCH_PAGE_JOB_FILTER,keywords:' + kw
    + ',selectedFilters:(' + filters.join(',') + '),spellCorrectionEnabled:true)';
  const r = await fetch(url, {headers: {
    'csrf-token': csrf,
    'accept': 'application/vnd.linkedin.normalized+json+2.1',
    'x-restli-protocol-version': '2.0.0',
  }});
  let total = null;
  try { total = (await r.json()).data.paging.total; } catch (e) {}
  return {status: r.status, total};
}"""


def _voyager_job_count(page, keywords: str, remote: bool, tpr: Optional[str] = None):
    """Exact LinkedIn job count for *keywords*, or None if the API did not answer."""
    try:
        res = page.evaluate(
            _VOYAGER_JOB_COUNT_JS, {"keywords": keywords, "remote": remote, "tpr": tpr}
        )
    except Exception as exc:
        log.warning("   Job count API call failed: %s", exc)
        return None
    if res.get("status") != 200 or not isinstance(res.get("total"), int):
        log.warning("   Job count API returned status %s", res.get("status"))
        return None
    return res["total"]


def _api_counts(pg, title_kw: str, remote: bool):
    """(total, past week, past day) counts from the job-search API."""
    if "linkedin.com" not in (pg.url or ""):
        pg.goto("https://www.linkedin.com/jobs/", wait_until="domcontentloaded", timeout=30000)
        _ensure_logged_in(pg, "https://www.linkedin.com/jobs/")
    counts: List[Optional[int]] = []
    for tpr in (None, "r604800", "r86400"):
        if counts:
            pg.wait_for_timeout(int(random.uniform(0.8, 2.0) * 1000))
        counts.append(_voyager_job_count(pg, title_kw, remote, tpr))
    return tuple(counts)


def _logged_api_counts(pg, title_kw: str, remote: bool):
    """API counts for one snapshot title, logged; None if the API gave nothing."""
    total_count, week_count, day_count = _api_counts(pg, title_kw, remote)
    if (total_count, week_count, day_count) == (None, None, None):
        log.warning("   Job count API gave nothing; reading the results pages")
        return None
    log.info(f"   Total results: {total_count or 'unknown'}")
    log.info(f"   Past week:     {week_count or 'unknown'}")
    log.info(f"   Past 24 hours: {day_count or 'unknown'}")
    return total_count, week_count, day_count


def _title_counts(pg, title_kw: str, base_params: Dict[str, str], location, remote: bool):
    """(total, past week, past day) for one snapshot title.

    The API needs no location lookup; a location search still goes through
    the results pages, as does any title the API does not answer.
    """
    if not location:
        counts = _logged_api_counts(pg, title_kw, remote)
        if counts:
            return counts
    return _scraped_counts(pg, base_params)


def _scraped_counts(pg, base_params: Dict[str, str]):
    """(total, past week, past day) counts read from three results pages."""
    from urllib.parse import urlencode

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
    return total_count, week_count, day_count


def _extract_results_count(page) -> Optional[int]:
    """Extract the total results count from a LinkedIn job search page."""
    # fmt: off
    # The first four selectors are the authenticated jobs-search DOM. The last
    # is the logged-out/guest jobs view (results-context-header__job-count),
    # which LinkedIn serves to fresh or untrusted sessions; keep it as the final
    # fallback so an authenticated count is always preferred when available.
    text = page.evaluate(
        "() => {"
        "  const selectors = ["
        "    '.jobs-search-results-list__subtitle',"
        "    '.jobs-search-results-list__title-heading small',"
        "    'header .jobs-search-results-list__text',"
        "    '.jobs-search-no-results-banner',"
        "    '.results-context-header__job-count',"
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


def _count_failed_snapshots(snapshots: List[Dict]) -> int:
    """Count snapshots where every result count is None (extraction failed).

    A title with all three counts missing means LinkedIn returned no readable
    results page for it, the usual signature of an expired session.
    """
    return sum(
        1
        for s in snapshots
        if s.get("total_results") is None
        and s.get("past_week_results") is None
        and s.get("past_day_results") is None
    )


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

    Returns list of snapshot entries saved to search_log.json, or an empty
    list when every title failed (so callers can exit non-zero).
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        raise RuntimeError("Run: pip install playwright && playwright install chromium") from None

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
                return (pg, *_title_counts(pg, title_kw, base_params, location, remote))
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

    failed = _count_failed_snapshots(snapshots)
    if snapshots and failed == len(snapshots):
        log.error(
            "All %d market snapshot titles returned no counts. "
            "LinkedIn session likely expired; re-authenticate and retry.",
            failed,
        )
        return []
    if failed:
        log.warning("%d of %d market snapshot titles returned no counts", failed, len(snapshots))
    return snapshots
