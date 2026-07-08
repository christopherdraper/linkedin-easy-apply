"""Command-line entry point: argument parsing, credential setup, LinkedIn
profile sync, and dispatch to the auto-apply workflow."""

import argparse
import hashlib
import json
import logging
import random
import re
import time
from pathlib import Path

from jobapply import stats
from jobapply.ai import _AI_AVAILABLE, _get_ai_client
from jobapply.browser import (
    _ensure_logged_in,
    _load_credentials,
    _playwright_context,
    _save_credentials,
    _stealth_playwright,
)
from jobapply.config import CREDENTIALS_FILE, DATA_DIR
from jobapply.external import submit_external_apply
from jobapply.profile import ApplicantProfile, JobSearchParams
from jobapply.queue import (
    _generate_deep_apply_prompt,
    _load_deep_apply_queue,
    _mark_deep_apply_done,
)
from jobapply.search import market_snapshot
from jobapply.workflow import auto_apply_workflow

log = logging.getLogger(__name__)


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
