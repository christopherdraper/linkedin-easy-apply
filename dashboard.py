#!/usr/bin/env python3
"""
Dashboard for tracking LinkedIn Easy Apply job applications.

Reads the application log and displays:
  - Application status table (searchable, sortable)
  - Job role hit counts: all time, last 2 weeks, last month

Usage:
    pip install flask
    python dashboard.py              # opens http://localhost:5050
    python dashboard.py --port 8080  # custom port
"""

import argparse
import json
import webbrowser
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from threading import Timer

from flask import Flask, render_template

DATA_DIR = Path.home() / ".local" / "share" / "job-apply"
LOG_FILE = DATA_DIR / "applications.json"

app = Flask(__name__)


def _load_applications():
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text())
        except Exception:
            return []
    return []


def _parse_ts(ts_str):
    """Parse a timestamp string from the application log."""
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _build_role_stats(entries):
    """Aggregate application counts per job title across time windows."""
    now = datetime.now()
    two_weeks_ago = now - timedelta(days=14)
    one_month_ago = now - timedelta(days=30)

    total = Counter()
    last_two_weeks = Counter()
    last_month = Counter()

    for entry in entries:
        title = entry.get("title", "Unknown")
        total[title] += 1

        ts = _parse_ts(entry.get("timestamp", ""))
        if ts:
            if ts >= two_weeks_ago:
                last_two_weeks[title] += 1
            if ts >= one_month_ago:
                last_month[title] += 1

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
    role_stats = _build_role_stats(entries)
    status_counts = _build_status_counts(entries)

    # Sort entries by timestamp descending (most recent first)
    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

    return render_template(
        "dashboard.html",
        entries=entries,
        role_stats=role_stats,
        status_counts=status_counts,
        total_applications=len(entries),
        log_file=str(LOG_FILE),
    )


@app.route("/api/data")
def api_data():
    """JSON endpoint for live-refresh without full page reload."""
    entries = _load_applications()
    role_stats = _build_role_stats(entries)
    status_counts = _build_status_counts(entries)
    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return {
        "entries": entries,
        "role_stats": role_stats,
        "status_counts": status_counts,
        "total_applications": len(entries),
    }


def main():
    parser = argparse.ArgumentParser(description="LinkedIn Easy Apply Dashboard")
    parser.add_argument("--port", type=int, default=5050, help="Port to run on (default: 5050)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    if not args.no_browser:
        Timer(1.0, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()

    print(f"\n📊 Dashboard: http://localhost:{args.port}")
    print(f"📁 Reading from: {LOG_FILE}\n")

    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
