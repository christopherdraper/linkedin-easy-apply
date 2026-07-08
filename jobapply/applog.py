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


def _preserve_corrupt_log(path) -> None:
    """Rename an unreadable log aside instead of silently overwriting it.

    applications.json is the full application history and feeds the
    already_applied dedup; losing it silently means re-applying to
    everything. A timestamped .corrupt sibling keeps the bytes around
    for manual recovery.
    """
    backup = path.with_name(path.name + ".corrupt")
    try:
        path.rename(backup)
        log.warning(f"⚠️  {path.name} was unreadable; preserved as {backup.name}")
    except OSError as e:
        log.warning(f"⚠️  {path.name} unreadable and backup failed ({e}); overwriting")


def save_log(entries: List[Dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = load_log()
    if not existing and LOG_FILE.exists():
        raw = LOG_FILE.read_text().strip()
        if raw:
            try:
                json.loads(raw)
            except Exception:
                _preserve_corrupt_log(LOG_FILE)
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
        try:
            existing: List[Dict] = json.loads(raw) if raw.strip() else []
        except Exception:
            # Same protection as save_log: preserve the corrupt file and
            # start fresh instead of crashing every snapshot run.
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
            _preserve_corrupt_log(SEARCH_LOG_FILE)
            fd = open(SEARCH_LOG_FILE, "a+")
            fcntl.flock(fd, fcntl.LOCK_EX)
            existing = []
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
