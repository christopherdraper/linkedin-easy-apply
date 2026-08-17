"""Command-line entry point: argument parsing, credential setup, LinkedIn
profile sync, and dispatch to the auto-apply workflow."""

import argparse
import hashlib
import json
import logging
import random
import re
import sys
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


# Once a profile page reaches one of these, only recommendations/footer noise follows.
_PROFILE_NOISE_MARKERS = (
    "Who your viewers also viewed",
    "People you may know",
    "More profiles for you",
    "You might like",
    "Pages for you",
    "About\nAccessibility",
)


def _profile_page_text(page, url: str) -> str:
    """Load a LinkedIn profile (detail) page and return its de-noised main text.

    Reads rendered innerText rather than specific component selectors: LinkedIn
    rewrites its profile DOM often (e.g. the 2026 server-driven-UI overhaul broke
    the old #experience / artdeco-list__item selectors), but the visible text is
    stable. Trims the recommendations/footer tail so only profile content remains.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    _ensure_logged_in(page, url)
    page.wait_for_timeout(3500)
    for _ in range(4):
        page.evaluate("window.scrollBy(0, 900)")
        page.wait_for_timeout(600)
    txt = page.evaluate("(document.querySelector('main') || document.body).innerText") or ""
    # Collapse blanks and drop consecutive duplicates (LinkedIn repeats a11y text).
    deduped: list = []
    for line in (ln.strip() for ln in txt.split("\n")):
        if line and (not deduped or deduped[-1] != line):
            deduped.append(line)
    text = "\n".join(deduped)
    cut = len(text)
    for marker in _PROFILE_NOISE_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return text[:cut].strip()


def _sync_linkedin_profile(profile_path: str) -> None:
    """Scrape the signed-in user's own LinkedIn profile and update profile.json."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        raise RuntimeError("Playwright not installed") from None

    raw = json.loads(Path(profile_path).expanduser().read_text())

    with _stealth_playwright() as p:
        browser, context, page, owns_browser = _playwright_context(p)
        try:
            # Resolve the signed-in member's OWN canonical profile. /in/me/ always
            # redirects to the logged-in user's profile, so we never scrape a
            # namesake even if personal.linkedin_url is wrong, stale, or missing.
            page.goto(
                "https://www.linkedin.com/in/me/", wait_until="domcontentloaded", timeout=30000
            )
            _ensure_logged_in(page, "https://www.linkedin.com/in/me/")
            page.wait_for_timeout(3000)
            profile_url = page.url.split("?")[0].rstrip("/")
            if "/in/" not in profile_url:
                log.error("❌ Could not resolve your own profile (landed on %s)", page.url)
                return
            log.info(f"🔄 Syncing your LinkedIn profile: {profile_url}")
            # Self-heal: persist the canonical URL back into the profile.
            raw.setdefault("profile", raw).setdefault("personal", {})["linkedin_url"] = profile_url

            exp_txt = _profile_page_text(page, profile_url + "/details/experience/")
            edu_txt = _profile_page_text(page, profile_url + "/details/education/")
            sk_txt = _profile_page_text(page, profile_url + "/details/skills/")
            main_txt = _profile_page_text(page, profile_url + "/")

            log.info(
                "   📋 experience %d chars · 🎓 education %d chars · 🛠️  skills %d chars",
                len(exp_txt),
                len(edu_txt),
                len(sk_txt),
            )
            if not (exp_txt or edu_txt):
                log.error("❌ No profile text extracted — LinkedIn may have blocked the session")
                return
            if not _AI_AVAILABLE:
                log.warning("⚠️  AI unavailable — cannot parse profile text. Raw experience:")
                print(exp_txt[:2000])
                return

            client = _get_ai_client()
            parse_prompt = f"""Parse this person's own LinkedIn profile into structured JSON.
The text below is the rendered content of their profile detail pages.

HEADLINE / ABOUT (top of profile):
{main_txt[:1800]}

EXPERIENCE:
{exp_txt[:4000]}

EDUCATION:
{edu_txt[:2000]}

SKILLS:
{sk_txt[:1500]}

Return ONLY valid JSON, no markdown fences, with this structure:
{{
  "current_title": "most recent / present job title",
  "current_employer": "most recent / present employer",
  "previous_employers": [
    {{"title": "", "employer": "", "dates": "Start - End", "industry": "", "description": "1 concise sentence"}}
  ],
  "skills": {{"programming_languages": [], "frameworks": [], "tools": []}},
  "education": {{"highest_degree": "", "field_of_study": "", "university": "", "graduation_year": 2021}},
  "specializations": [],
  "about": "1-2 sentence professional summary"
}}

Rules:
- The most recent role (marked Present/current) is current_title + current_employer; put every earlier role in previous_employers, most recent first (INCLUDING earlier roles at the same employer).
- highest_degree = the highest COMPLETED degree; a finished Bachelor's outranks incomplete graduate coursework.
- Categorize skills: languages (Python, MATLAB, ...) vs frameworks/software (CAD, simulation tools) vs tools/domain methods. Keep only real professional skills; drop noise.
- Output ONLY the JSON."""

            response = client.messages.create(
                model="claude-sonnet-5",
                thinking={"type": "disabled"},
                max_tokens=3000,
                messages=[{"role": "user", "content": parse_prompt}],
            )
            stats.add_ai_tokens(response.usage)
            parsed_text = response.content[0].text.strip()
            json_match = re.search(r"\{.*\}", parsed_text, re.DOTALL)
            if not json_match:
                log.error("❌ AI failed to return valid JSON")
                log.info("   Raw response: %s", parsed_text[:200])
                return
            _apply_synced_profile(raw, json.loads(json_match.group()), profile_path)
        finally:
            page.close()
            if owns_browser:
                browser.close()


def _apply_synced_profile(raw: dict, parsed: dict, profile_path: str) -> None:
    """Merge AI-parsed LinkedIn data into the existing profile.json."""
    p = raw.setdefault("profile", raw)
    exp = p.setdefault("experience", {})

    # Current role (the parser separates the present job from prior ones)
    if parsed.get("current_title"):
        exp["current_title"] = parsed["current_title"]
    if parsed.get("current_employer"):
        exp["current_employer"] = parsed["current_employer"]

    # Prior roles, most recent first (already excludes the current role above)
    if parsed.get("previous_employers"):
        prev = []
        for job in parsed["previous_employers"]:
            entry = {"title": job.get("title", ""), "employer": job.get("employer", "")}
            for key in ("industry", "dates", "description"):
                if job.get(key):
                    entry[key] = job[key]
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

    # Professional summary (used for resume/cover-letter context)
    if parsed.get("about"):
        p["summary"] = parsed["about"]

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


def _handle_deep_apply_cli(args, parser) -> None:
    """Handle the --deep-apply subcommands: list, prompt, prompt-all, done."""
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


def _run_market_snapshot(args, parser) -> None:
    """Handle the --market-snapshot branch: count job postings per title."""
    _raw = json.loads(Path(args.profile).expanduser().read_text())
    _criteria = _raw.get("search_criteria", {})
    _prefs = _raw.get("profile", _raw).get("preferences", {})
    titles = args.title if args.title else _criteria.get("job_titles", [])
    if not titles:
        parser.error("No job titles — pass --title or set search_criteria.job_titles in profile")
    if args.remote is None:
        work_arrangement = _prefs.get("work_arrangement", ["remote"])
        remote = "remote" in work_arrangement
    else:
        remote = args.remote
    snapshots = market_snapshot(titles, location=args.location, remote=remote, proxy=args.proxy)
    if not snapshots:
        # Every title failed (or nothing was scanned): exit non-zero so cron
        # surfaces the failure instead of silently recording junk.
        sys.exit(1)


def _abort_if_setup_incomplete(profile: ApplicantProfile) -> None:
    """Fail fast before any apply run if the profile is misconfigured."""
    err = profile.apply_prerequisite_error()
    if err:
        log.error("❌ %s", err)
        sys.exit(1)


def _run_external_url(args) -> None:
    """Handle the --external-url branch: apply to a single external job URL."""
    _raw = json.loads(Path(args.profile).expanduser().read_text())
    profile = ApplicantProfile.from_dict(_raw)
    _abort_if_setup_incomplete(profile)
    job = {
        "id": f"ext_{hashlib.sha256(args.external_url.encode()).hexdigest()[:12]}",
        "url": args.external_url,
        "title": args.job_title or "Unknown",
        "company": args.company or "Unknown",
        "description": "",
        "apply_type": "external",
    }
    log.info(f"🌐 Applying to external URL: {args.external_url}")
    stats.reset_run_stats()
    status = submit_external_apply(
        job,
        profile,
        proxy=args.proxy,
        dry_run=args.dry_run,
    )
    log.info(f"Result: {status}")


def _resolve_batch_settings(args, parser):
    """Resolve batch-run settings from CLI args and the profile file."""
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

    return profile, _criteria, max_applications, min_score, titles, remote


def _run_batch(args, profile, _criteria, max_applications, min_score, titles, remote) -> None:
    """Run the default batch loop over sources and titles."""
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


def main():
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
        _handle_deep_apply_cli(args, parser)
        return

    if args.setup:
        _run_setup()
        return

    if args.sync_profile:
        _sync_linkedin_profile(args.profile)
        return

    if args.market_snapshot:
        _run_market_snapshot(args, parser)
        return

    if args.external_url:
        _run_external_url(args)
        return

    # Auto-sync profile if stale (>7 days since last sync)
    _maybe_sync_profile(args.profile)

    profile, _criteria, max_applications, min_score, titles, remote = _resolve_batch_settings(
        args, parser
    )
    _abort_if_setup_incomplete(profile)
    _run_batch(args, profile, _criteria, max_applications, min_score, titles, remote)
