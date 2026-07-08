"""Job search sources (LinkedIn, RemoteOK, HackerNews, biotech Workday sites)
and the LinkedIn market snapshot scan."""

import hashlib
import json
import logging
import random
import re
import time
from typing import Dict, List, Optional

from jobapply.applog import save_search_log
from jobapply.browser import (
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
