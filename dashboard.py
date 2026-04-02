#!/usr/bin/env python3
"""
Dashboard for tracking LinkedIn Easy Apply job applications.

Reads the application log and search log to display:
  - Application status table (searchable, sortable)
  - Total jobs seen per role: all time, last 2 weeks, last month
  - Application status breakdown

Usage:
    pip install flask
    python dashboard.py              # opens http://localhost:5050
    python dashboard.py --port 8080  # custom port
"""

import argparse
import json
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from threading import Timer

from flask import Flask, render_template

DATA_DIR = Path.home() / ".local" / "share" / "job-apply"
LOG_FILE = DATA_DIR / "applications.json"
SEARCH_LOG_FILE = DATA_DIR / "search_log.json"

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


def _build_role_stats(search_entries):
    """Aggregate total jobs *seen* per search title across time windows.

    Each search_log entry has: search_title, source, jobs_found, timestamp.
    We sum jobs_found per search_title for each time window.
    """
    now = datetime.now()
    two_weeks_ago = now - timedelta(days=14)
    one_month_ago = now - timedelta(days=30)

    total = defaultdict(int)
    last_two_weeks = defaultdict(int)
    last_month = defaultdict(int)

    for entry in search_entries:
        title = entry.get("search_title", "Unknown")
        count = entry.get("jobs_found", 0)
        total[title] += count

        ts = _parse_ts(entry.get("timestamp", ""))
        if ts:
            if ts >= two_weeks_ago:
                last_two_weeks[title] += count
            if ts >= one_month_ago:
                last_month[title] += count

    # Sort by total count descending
    all_roles = sorted(total.keys(), key=lambda r: total[r], reverse=True)

    return {
        "roles": all_roles,
        "total": [total[r] for r in all_roles],
        "last_two_weeks": [last_two_weeks.get(r, 0) for r in all_roles],
        "last_month": [last_month.get(r, 0) for r in all_roles],
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


@app.route("/")
def index():
    entries = _load_applications()
    search_entries = _load_search_log()
    role_stats = _build_role_stats(search_entries)
    status_counts = _build_status_counts(entries)

    # Sort entries by timestamp descending (most recent first)
    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

    total_jobs_seen = sum(e.get("jobs_found", 0) for e in search_entries)

    return render_template(
        "dashboard.html",
        entries=entries,
        role_stats=role_stats,
        status_counts=status_counts,
        total_applications=len(entries),
        total_jobs_seen=total_jobs_seen,
        total_searches=len(search_entries),
        log_file=str(LOG_FILE),
    )


@app.route("/api/data")
def api_data():
    """JSON endpoint for live-refresh without full page reload."""
    entries = _load_applications()
    search_entries = _load_search_log()
    role_stats = _build_role_stats(search_entries)
    status_counts = _build_status_counts(entries)
    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    total_jobs_seen = sum(e.get("jobs_found", 0) for e in search_entries)
    return {
        "entries": entries,
        "role_stats": role_stats,
        "status_counts": status_counts,
        "total_applications": len(entries),
        "total_jobs_seen": total_jobs_seen,
        "total_searches": len(search_entries),
    }


def main():
    parser = argparse.ArgumentParser(description="LinkedIn Easy Apply Dashboard")
    parser.add_argument("--port", type=int, default=5050, help="Port to run on (default: 5050)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    if not args.no_browser:
        Timer(1.0, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()

    print(f"\n📊 Dashboard: http://localhost:{args.port}")
    print(f"📁 Reading from: {LOG_FILE}")
    print(f"📁 Search log:   {SEARCH_LOG_FILE}\n")

    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
