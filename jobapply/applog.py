"""Application and search log persistence (applications.json, search_log.json)."""

import json
import logging
import re
from typing import Dict, List

from jobapply.config import DATA_DIR, LOG_FILE, SEARCH_LOG_FILE

log = logging.getLogger(__name__)


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
