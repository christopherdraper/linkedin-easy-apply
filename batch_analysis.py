#!/usr/bin/env python3
"""
Post-batch failure analysis.

Analyzes application failures from applications.json, identifies recurring
patterns, and creates GitHub issues for actionable bugs.

Usage:
    python batch_analysis.py                  # analyze all, create issues for new patterns
    python batch_analysis.py --dry-run        # print analysis only, don't create issues
    python batch_analysis.py --since 2026-04-05  # only analyze apps from this date
"""

import argparse
import json
import logging
import re
import subprocess  # nosec B404
from collections import defaultdict
from pathlib import Path

log = logging.getLogger(__name__)

DATA_DIR = Path.home() / ".local" / "share" / "job-apply"
LOG_FILE = DATA_DIR / "applications.json"
REPO = "christopherdraper/linkedin-easy-apply"


def _load_applications(since: str | None = None) -> list[dict]:
    if not LOG_FILE.exists():
        return []
    apps = json.loads(LOG_FILE.read_text())
    if since:
        # Keep entries with no timestamp: they cannot be proven older than
        # `since`, and silently dropping them would hide failures from analysis.
        apps = [a for a in apps if not a.get("timestamp") or a["timestamp"] >= since]
    return apps


def _extract_validation_fields(status: str) -> list[str]:
    """Extract specific field names from validation error messages."""
    # Pattern: "field name*; error message; field name*; error message"
    # or "field name; Please enter..."
    parts = status.split(";")
    fields = []
    for part in parts:
        part = part.strip()
        # Skip generic error messages
        if any(
            skip in part.lower()
            for skip in [
                "please enter",
                "this field is required",
                "select",
                "required",
                "there are problems",
                "is required",
                "page is loaded",
                "we couldn't submit",
            ]
        ):
            continue
        # Skip the "failed:" prefix
        part = re.sub(r"^failed:\s*", "", part)
        part = re.sub(r"^form validation errors?:\s*", "", part)
        if part and len(part) < 80:
            fields.append(part)
    return fields


def _detect_ats_platform(app: dict) -> str:
    """Detect ATS platform from debug HTML files or app metadata."""
    ats = app.get("ats_platform", "")
    if ats and ats != "unknown":
        return ats

    job_id = app.get("job_id", "")
    debug_dir = DATA_DIR / "debug"

    # Check debug HTML files for ATS markers
    for html_file in sorted(debug_dir.glob(f"*{job_id}*.html")):
        try:
            content = html_file.read_text()[:10000]
        except Exception:
            continue
        if "greenhouse" in content.lower():
            return "greenhouse"
        if "ashby" in content.lower():
            return "ashby"
        if "lever" in content.lower():
            return "lever"
        if "workday" in content.lower():
            return "workday"
        if "icims" in content.lower():
            return "icims"
        if "taleo" in content.lower():
            return "taleo"

    # Check URL patterns
    url = app.get("url", "")
    if "greenhouse" in url:
        return "greenhouse"
    if "ashby" in url:
        return "ashby"
    if "lever" in url:
        return "lever"

    return "unknown"


def _fingerprint_failure(app: dict) -> str:  # noqa: C901
    """Create a failure fingerprint for grouping similar failures."""
    cat = app.get("failure_category", "other")
    status = app.get("status", "")
    ats = _detect_ats_platform(app)

    if cat == "validation_error":
        fields = _extract_validation_fields(status)
        if fields:
            # Normalize field names for grouping
            normalized = sorted(set(f.lower().rstrip("*").strip() for f in fields))
            return f"{ats}:validation:{','.join(normalized[:3])}"
        # Try to extract a meaningful fragment from the raw status
        if "spam" in status.lower():
            return f"{ats}:validation:spam_detection"
        if "page is loaded" in status.lower():
            return f"{ats}:validation:page_not_interactive"
        return f"{ats}:validation:unknown_fields"

    if cat == "form_stuck":
        # Extract step info
        step_match = re.search(r"step (\d+)/(\d+)", status)
        if step_match:
            step = int(step_match.group(1))
            if step <= 3:
                return f"{ats}:form_stuck:early_stall"
            return f"{ats}:form_stuck:mid_form"
        if "same page" in status:
            return f"{ats}:form_stuck:same_page_loop"
        return f"{ats}:form_stuck"

    if cat == "max_steps":
        return f"{ats}:max_steps"

    if cat == "captcha":
        return f"{ats}:captcha"

    if cat == "no_apply_button":
        return f"{ats}:no_apply_button"

    # Break down "other"/uncategorized into more specific categories
    s = status.lower()
    if cat in ("other", None, ""):
        if "invalid url" in s or "cannot navigate" in s:
            return f"{ats}:invalid_url"
        if "no next/submit" in s or "no button" in s:
            return f"{ats}:no_submit_button"
        if "job search page" in s or "listing page" in s:
            return f"{ats}:landed_on_search_page"
        if "requires account" in s or "login" in s:
            return f"{ats}:login_wall"
        if "file exceeds" in s or "upload" in s:
            return f"{ats}:upload_error"
        if "spam" in s or "couldn't submit" in s:
            return f"{ats}:spam_detection"
        if "missing entry" in s or "required field" in s or "missing required" in s:
            return f"{ats}:missing_required_field"
        if "no apply button" in s or "easy apply button not found" in s:
            return f"{ats}:no_apply_button"
        if "same page" in s or "not advancing" in s or "form stuck" in s:
            return f"{ats}:form_stuck:stall_loop"
        if "lost track" in s:
            return f"{ats}:form_stuck:lost_track"
        if "max form steps" in s or "exceeded max" in s:
            return f"{ats}:max_steps"
        if "timeout" in s or "timed out" in s:
            return f"{ats}:timeout"
        if "captcha" in s:
            return f"{ats}:captcha"

    return f"{ats}:{cat or 'uncategorized'}"


def _get_existing_issues() -> dict[str, dict] | None:
    """Get existing open issues from GitHub, keyed by fingerprint in title.

    Returns None when the lookup fails: callers must treat that as "dedup
    unavailable" and skip issue creation (duplicates are worse than a skip).
    """
    try:
        result = subprocess.run(  # nosec B603 B607
            [
                "gh",
                "issue",
                "list",
                "--repo",
                REPO,
                "--state",
                "open",
                "--label",
                "batch-failure",
                "--json",
                "title,number,url",
                "--limit",
                "100",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        issues = json.loads(result.stdout)
        existing = {}
        for issue in issues:
            # Extract fingerprint from title: "[fingerprint] description"
            match = re.match(r"\[([^\]]+)\]", issue["title"])
            if match:
                existing[match.group(1)] = issue
        return existing
    except Exception as e:
        log.warning("Could not fetch existing GitHub issues (dedup unavailable): %s", e)
        return None


def _create_issue(fingerprint: str, apps: list[dict], dry_run: bool = False) -> str | None:
    """Create a GitHub issue for a failure pattern."""
    ats = fingerprint.split(":")[0]
    category = fingerprint.split(":")[1] if ":" in fingerprint else "unknown"

    # Build title
    if category == "validation":
        detail = fingerprint.split("validation:")[-1]
        title = f"[{fingerprint}] {ats.title()} form validation: {detail}"
    elif category == "form_stuck":
        detail = (
            fingerprint.split("form_stuck:")[-1]
            if ":" in fingerprint.split("form_stuck", 1)[-1]
            else ""
        )
        title = f"[{fingerprint}] {ats.title()} form stuck{': ' + detail if detail else ''}"
    elif category == "max_steps":
        title = f"[{fingerprint}] {ats.title()} exceeded max form steps"
    else:
        title = f"[{fingerprint}] {ats.title()} {category}"

    # Truncate title
    if len(title) > 120:
        title = title[:117] + "..."

    # Build body
    lines = []
    lines.append("## Failure Pattern\n")
    lines.append(f"- **ATS Platform**: {ats}")
    lines.append(f"- **Category**: {category}")
    lines.append(f"- **Fingerprint**: `{fingerprint}`")
    lines.append(f"- **Affected applications**: {len(apps)}")
    lines.append(
        f"- **Score range**: {min(a.get('match_score', 0) for a in apps):.2f} – {max(a.get('match_score', 0) for a in apps):.2f}"
    )
    lines.append("")

    lines.append("## Sample Error Messages\n")
    seen = set()
    for a in apps[:5]:
        status = a.get("status", "")
        if status not in seen:
            lines.append(f"- `{status[:120]}`")
            seen.add(status)
    lines.append("")

    lines.append("## Affected Jobs\n")
    lines.append("| Job ID | Company | Title | Score | Date |")
    lines.append("|--------|---------|-------|-------|------|")
    for a in sorted(apps, key=lambda x: x.get("match_score", 0), reverse=True)[:15]:
        job_id = a.get("job_id", "?")
        company = a.get("company", "?")[:20]
        title_text = a.get("title", "?").split("\n")[0][:35]
        score = f"{a.get('match_score', 0):.2f}"
        ts = a.get("timestamp", "?")[:10]
        lines.append(f"| {job_id} | {company} | {title_text} | {score} | {ts} |")
    lines.append("")

    # Debug info
    lines.append("## Debug Files\n")
    debug_dir = DATA_DIR / "debug"
    for a in apps[:3]:
        job_id = a.get("job_id", "")
        htmls = sorted(debug_dir.glob(f"*{job_id}*.html"))
        pngs = sorted(debug_dir.glob(f"*{job_id}*.png"))
        if htmls or pngs:
            lines.append(f"**{job_id}**:")
            for f in htmls[:1]:
                lines.append(f"- HTML: `{f}`")
            for f in pngs[:1]:
                lines.append(f"- Screenshot: `{f}`")
            lines.append("")

    lines.append("## Investigation Notes\n")
    lines.append("<!-- Add root cause analysis here -->\n")

    body = "\n".join(lines)

    if dry_run:
        print(f"\n{'=' * 60}")
        print(f"WOULD CREATE: {title}")
        print(f"{'=' * 60}")
        print(body[:500])
        print("...")
        return None

    try:
        result = subprocess.run(  # nosec B603 B607
            [
                "gh",
                "issue",
                "create",
                "--repo",
                REPO,
                "--title",
                title,
                "--body",
                body,
                "--label",
                "batch-failure,bug",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        url = result.stdout.strip()
        print(f"  ✅ Created: {url}")
        return url
    except subprocess.CalledProcessError as e:
        print(f"  ❌ Failed to create issue: {e.stderr}")
        return None


def analyze(since: str | None = None, dry_run: bool = False, min_count: int = 2):
    """Analyze failures and create issues for recurring patterns."""
    apps = _load_applications(since)
    failed = [a for a in apps if a.get("status", "").startswith("failed")]

    if not failed:
        print("No failures to analyze.")
        return

    print(f"\n📊 Analyzing {len(failed)} failures" + (f" since {since}" if since else ""))

    # Group by fingerprint
    groups: dict[str, list[dict]] = defaultdict(list)
    for a in failed:
        fp = _fingerprint_failure(a)
        groups[fp].append(a)

    # Sort by count descending
    sorted_groups = sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)

    print(f"   {len(sorted_groups)} distinct failure patterns\n")

    # Summary table
    print(f"{'Pattern':<45} {'Count':>5}  {'Avg Score':>9}  {'Example Company'}")
    print("-" * 95)
    for fp, group in sorted_groups:
        avg_score = sum(a.get("match_score", 0) for a in group) / len(group)
        example = group[0].get("company", "?")[:20]
        print(f"{fp:<45} {len(group):>5}  {avg_score:>8.2f}  {example}")

    # Get existing GitHub issues. None means the lookup failed: without the
    # dedup list we would file duplicates, so skip issue creation this run.
    existing = _get_existing_issues()

    if existing is None:
        print(
            "\n⚠️  Could not fetch existing GitHub issues (dedup unavailable). "
            "Skipping issue creation for this run."
        )
    else:
        # Create issues for patterns with enough occurrences
        actionable = [
            (fp, group)
            for fp, group in sorted_groups
            if len(group) >= min_count and fp not in existing
        ]

        if actionable:
            print(f"\n📝 {len(actionable)} new patterns to file (min {min_count} occurrences):\n")
            for fp, group in actionable:
                print(f"  [{len(group)}x] {fp}")
                _create_issue(fp, group, dry_run=dry_run)
        else:
            already = sum(1 for fp, _ in sorted_groups if fp in existing)
            print(f"\n✅ No new patterns to file ({already} already tracked)")

    # Flag high-value single failures (score >= 0.90). This section only
    # prints (no issue creation), so with dedup unavailable list them all.
    known = existing or {}
    high_value_singles = [
        (fp, group)
        for fp, group in sorted_groups
        if len(group) == 1 and group[0].get("match_score", 0) >= 0.90 and fp not in known
    ]
    if high_value_singles:
        print(f"\n⚠️  {len(high_value_singles)} single high-value failures (score >= 0.90):")
        for fp, group in high_value_singles:
            a = group[0]
            print(
                f"  {a.get('match_score', 0):.2f} {a.get('company', '?'):15s} {a.get('title', '?').split(chr(10))[0][:40]}"
            )
            print(f"       {fp}")


def main():
    parser = argparse.ArgumentParser(description="Post-batch failure analysis")
    parser.add_argument("--since", help="Only analyze apps from this date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Print analysis only")
    parser.add_argument(
        "--min-count", type=int, default=2, help="Minimum failures to create an issue (default: 2)"
    )
    args = parser.parse_args()

    analyze(since=args.since, dry_run=args.dry_run, min_count=args.min_count)


if __name__ == "__main__":
    main()
