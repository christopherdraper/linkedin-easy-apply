#!/usr/bin/env python3
"""
Dashboard for tracking LinkedIn Easy Apply job applications.

Reads the application log and market snapshot log to display:
  - Market pulse: total job postings per role (total vs past week vs past 24h)
  - Application status table (searchable, sortable)
  - Application status breakdown

Usage:
    pip install flask
    python dashboard.py              # opens http://localhost:5050
    python dashboard.py --port 8080  # custom port
"""

import argparse
import json
import sys
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from threading import Timer

from flask import Flask, abort, render_template

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_search_apply import ApplicantProfile, _generate_deep_apply_prompt  # noqa: E402

DATA_DIR = Path.home() / ".local" / "share" / "job-apply"
LOG_FILE = DATA_DIR / "applications.json"
SEARCH_LOG_FILE = DATA_DIR / "search_log.json"
DEEP_APPLY_QUEUE_FILE = DATA_DIR / "deep_apply_queue.json"

app = Flask(__name__)


def _load_json(path):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return []
    return []


def _load_applications():
    return _load_json(LOG_FILE)


def _load_search_log():
    return _load_json(SEARCH_LOG_FILE)


def _parse_ts(ts_str):
    """Parse a timestamp string from the log."""
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _build_market_stats(search_entries):
    """Build market data from search_log.json snapshots.

    Each snapshot has: search_title, total_results, past_week_results,
    past_day_results, timestamp.
    We take the most recent snapshot per role for the chart, and keep the full
    history for the timeline.
    """
    # Group by search_title, keep most recent snapshot per title
    latest = {}
    history = defaultdict(list)

    for entry in search_entries:
        title = entry.get("search_title", "Unknown")
        ts = _parse_ts(entry.get("timestamp", ""))
        history[title].append(entry)
        existing_ts = _parse_ts(latest[title].get("timestamp", "")) if title in latest else None
        if title not in latest or (ts and (not existing_ts or ts > existing_ts)):
            latest[title] = entry

    # Sort roles by total results descending
    roles = sorted(latest.keys(), key=lambda r: latest[r].get("total_results") or 0, reverse=True)

    return {
        "roles": roles,
        "total_results": [latest[r].get("total_results") or 0 for r in roles],
        "past_week": [latest[r].get("past_week_results") or 0 for r in roles],
        "past_day": [latest[r].get("past_day_results") or 0 for r in roles],
        "latest": {r: latest[r] for r in roles},
        "history": dict(history),
    }


def _build_status_counts(entries):
    """Count applications by status category."""
    counts = Counter()
    for entry in entries:
        status = entry.get("status", "unknown")
        if status.startswith("submitted"):
            counts["Submitted"] += 1
        elif status.startswith("aborted"):
            counts["Aborted"] += 1
        elif status.startswith("failed"):
            counts["Failed"] += 1
        elif status == "dry_run":
            counts["Dry Run"] += 1
        else:
            counts["Other"] += 1
    return dict(counts)


def _compute_cost_stats(entries):
    """Compute cost totals/averages and backfill cost_usd on historical entries."""
    total_cost = 0.0
    submitted_costs = []
    failed_costs = []
    for e in entries:
        cost = e.get("cost_usd")
        if cost is None:
            tokens = e.get("ai_tokens", {})
            if tokens.get("input") or tokens.get("output"):
                cost = round(
                    (tokens.get("input", 0) * 2.40 / 1_000_000)
                    + (tokens.get("output", 0) * 12.00 / 1_000_000),
                    4,
                )
                e["cost_usd"] = cost  # backfill for template use
        if cost is not None:
            total_cost += cost
            if e.get("status", "").startswith("submitted"):
                submitted_costs.append(cost)
            elif e.get("status", "").startswith("failed"):
                failed_costs.append(cost)
    avg_submitted = (
        round(sum(submitted_costs) / len(submitted_costs), 4) if submitted_costs else 0.0
    )
    avg_failed = round(sum(failed_costs) / len(failed_costs), 4) if failed_costs else 0.0
    return round(total_cost, 2), avg_submitted, avg_failed


@app.route("/")
def index():
    entries = _load_applications()
    search_entries = _load_search_log()
    market_stats = _build_market_stats(search_entries)
    status_counts = _build_status_counts(entries)

    # Failure category breakdown
    failure_categories = Counter()
    for e in entries:
        cat = e.get("failure_category")
        if cat:
            failure_categories[cat] += 1

    # Success rate by ATS platform
    platform_stats = defaultdict(lambda: {"submitted": 0, "failed": 0})
    for e in entries:
        plat = e.get("ats_platform", "unknown")
        if not plat or plat == "unknown":
            continue
        if e.get("status", "").startswith("submitted"):
            platform_stats[plat]["submitted"] += 1
        elif e.get("status", "").startswith("failed"):
            platform_stats[plat]["failed"] += 1

    # Score distribution histogram (10 bins)
    score_bins = [0] * 10  # 0-10%, 10-20%, ..., 90-100%
    for e in entries:
        s = e.get("match_score")
        if s is not None and s > 0:
            idx = min(int(s * 10), 9)
            score_bins[idx] += 1

    # Hiring manager message stats
    hm_sent = sum(1 for e in entries if e.get("hiring_manager_messaged") == "sent")
    hm_eligible = sum(1 for e in entries if e.get("status", "").startswith("submitted"))

    total_cost, avg_cost_submitted, avg_cost_failed = _compute_cost_stats(entries)

    # Deep-apply queue
    deep_queue = _load_json(DEEP_APPLY_QUEUE_FILE)
    deep_pending = [q for q in deep_queue if q.get("status") == "pending"]
    deep_done = [q for q in deep_queue if q.get("status") == "done"]
    deep_success = sum(1 for q in deep_done if q.get("deep_apply_status") == "submitted")

    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

    return render_template(
        "dashboard.html",
        entries=entries,
        market_stats=market_stats,
        status_counts=status_counts,
        failure_categories=dict(failure_categories),
        platform_stats=dict(platform_stats),
        total_applications=len(entries),
        total_snapshots=len(search_entries),
        log_file=str(LOG_FILE),
        score_bins=score_bins,
        hm_sent=hm_sent,
        hm_eligible=hm_eligible,
        total_cost=round(total_cost, 2),
        avg_cost_submitted=avg_cost_submitted,
        avg_cost_failed=avg_cost_failed,
        deep_queue=deep_queue,
        deep_pending_count=len(deep_pending),
        deep_success_count=deep_success,
        deep_done_count=len(deep_done),
    )


@app.route("/report/<job_id>")
def report(job_id):
    entries = _load_applications()
    entry = next((e for e in entries if e.get("job_id") == job_id), None)
    if not entry:
        abort(404)

    cover_letter = ""
    cl_path = entry.get("cover_letter_path", "")
    if cl_path:
        p = Path(cl_path)
        if p.exists():
            try:
                cover_letter = p.read_text()
            except Exception:
                cover_letter = "(unable to read file)"

    return render_template("report.html", entry=entry, cover_letter=cover_letter)


@app.route("/deep-apply/<job_id>")
def deep_apply_prompt(job_id):
    queue = _load_json(DEEP_APPLY_QUEUE_FILE)
    entry = next((q for q in queue if q.get("job_id") == job_id), None)
    if not entry:
        abort(404)

    profile_path = DATA_DIR / "profile.json"
    profile = ApplicantProfile.from_dict(json.loads(profile_path.read_text()))
    prompt = _generate_deep_apply_prompt(entry, profile)

    return render_template("deep_apply_prompt.html", entry=entry, prompt=prompt)


@app.route("/api/data")
def api_data():
    """JSON endpoint for live-refresh without full page reload."""
    entries = _load_applications()
    search_entries = _load_search_log()
    market_stats = _build_market_stats(search_entries)
    status_counts = _build_status_counts(entries)

    failure_categories = Counter()
    for e in entries:
        cat = e.get("failure_category")
        if cat:
            failure_categories[cat] += 1

    platform_stats = defaultdict(lambda: {"submitted": 0, "failed": 0})
    for e in entries:
        plat = e.get("ats_platform", "unknown")
        if not plat or plat == "unknown":
            continue
        if e.get("status", "").startswith("submitted"):
            platform_stats[plat]["submitted"] += 1
        elif e.get("status", "").startswith("failed"):
            platform_stats[plat]["failed"] += 1

    score_bins = [0] * 10
    for e in entries:
        s = e.get("match_score")
        if s is not None and s > 0:
            idx = min(int(s * 10), 9)
            score_bins[idx] += 1

    hm_sent = sum(1 for e in entries if e.get("hiring_manager_messaged") == "sent")
    hm_eligible = sum(1 for e in entries if e.get("status", "").startswith("submitted"))

    total_cost, avg_cost_submitted, avg_cost_failed = _compute_cost_stats(entries)

    # Deep-apply queue
    deep_queue = _load_json(DEEP_APPLY_QUEUE_FILE)
    deep_pending = [q for q in deep_queue if q.get("status") == "pending"]
    deep_done = [q for q in deep_queue if q.get("status") == "done"]
    deep_success = sum(1 for q in deep_done if q.get("deep_apply_status") == "submitted")

    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return {
        "entries": entries,
        "market_stats": market_stats,
        "status_counts": status_counts,
        "failure_categories": dict(failure_categories),
        "platform_stats": dict(platform_stats),
        "total_applications": len(entries),
        "total_snapshots": len(search_entries),
        "score_bins": score_bins,
        "hm_sent": hm_sent,
        "hm_eligible": hm_eligible,
        "total_cost": round(total_cost, 2),
        "avg_cost_submitted": avg_cost_submitted,
        "avg_cost_failed": avg_cost_failed,
        "deep_pending_count": len(deep_pending),
        "deep_success_count": deep_success,
        "deep_done_count": len(deep_done),
    }


def main():
    parser = argparse.ArgumentParser(description="LinkedIn Easy Apply Dashboard")
    parser.add_argument("--port", type=int, default=5050, help="Port to run on (default: 5050)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    if not args.no_browser:
        Timer(1.0, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()

    print(f"\n📊 Dashboard: http://localhost:{args.port}")
    print(f"📁 Applications: {LOG_FILE}")
    print(f"📁 Market data:  {SEARCH_LOG_FILE}\n")

    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
