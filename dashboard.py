#!/usr/bin/env python3
"""
Dashboard for tracking LinkedIn Easy Apply job applications.

Reads the application log and market snapshot log to display:
  - Market pulse: total job postings per role (all results vs past 2 weeks)
  - Application status table (searchable, sortable)
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
from datetime import datetime
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


def _build_market_stats(search_entries):
    """Build market data from search_log.json snapshots.

    Each snapshot has: search_title, total_results, past_two_weeks_results, timestamp.
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
        "past_two_weeks": [latest[r].get("past_two_weeks_results") or 0 for r in roles],
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


@app.route("/")
def index():
    entries = _load_applications()
    search_entries = _load_search_log()
    market_stats = _build_market_stats(search_entries)
    status_counts = _build_status_counts(entries)

    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

    return render_template(
        "dashboard.html",
        entries=entries,
        market_stats=market_stats,
        status_counts=status_counts,
        total_applications=len(entries),
        total_snapshots=len(search_entries),
        log_file=str(LOG_FILE),
    )


@app.route("/api/data")
def api_data():
    """JSON endpoint for live-refresh without full page reload."""
    entries = _load_applications()
    search_entries = _load_search_log()
    market_stats = _build_market_stats(search_entries)
    status_counts = _build_status_counts(entries)
    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return {
        "entries": entries,
        "market_stats": market_stats,
        "status_counts": status_counts,
        "total_applications": len(entries),
        "total_snapshots": len(search_entries),
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
