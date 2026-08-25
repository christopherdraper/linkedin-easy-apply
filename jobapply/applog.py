"""Application and search log persistence (applications.json, search_log.json)."""

import json
import logging
import re
import time
from typing import Dict, List

from jobapply.config import DATA_DIR, LOG_FILE, SCORE_CACHE_FILE, SEARCH_LOG_FILE

log = logging.getLogger(__name__)

# A cached rejection older than this is re-scored: postings get edited, and the
# profile itself re-syncs from LinkedIn every 7 days, so a stale verdict should
# not blacklist a job forever.
SCORE_CACHE_TTL_DAYS = 14


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


# Statuses that mean "this job has had its turn" -- a later batch must not
# spend another run on it. "review_parked" belongs here: the Workday draft
# already exists server-side and only the human can submit it, so re-driving it
# just burns a vision pass (~$1.20 and ~3 minutes) and adds a duplicate row.
_ATTEMPTED_STATUSES = ("submitted", "failed", "skipped", "review_parked")


def already_applied(log_entries: List[Dict]) -> set:
    """Return a set of canonical URLs and job IDs for previously attempted applications.

    Covers submitted, failed, skipped and review-parked entries so we don't
    re-attempt the same job with a different tracking-parameter URL.
    """
    result = set()
    for e in log_entries:
        status = e.get("status", "")
        if status.startswith(_ATTEMPTED_STATUSES):
            if e.get("url"):
                # Strip tracking params for canonical matching
                canonical = re.sub(r"\?.*$", "", e["url"])
                result.add(canonical)
                result.add(e["url"])
            if e.get("job_id"):
                result.add(e["job_id"])
    return result


def load_score_cache() -> Dict[str, Dict]:
    """Load the job_id -> scoring-verdict cache.

    This is a pure performance cache: losing it only costs redundant AI calls,
    never application history, so a corrupt file is discarded rather than
    preserved the way applications.json is.
    """
    if SCORE_CACHE_FILE.exists():
        try:
            data = json.loads(SCORE_CACHE_FILE.read_text())
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_score_cache(cache: Dict[str, Dict]) -> None:
    """Persist the whole cache (upsert-by-key, NOT append like save_log)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SCORE_CACHE_FILE.write_text(json.dumps(cache, indent=2))


def cached_score(cache: Dict[str, Dict], job_id: str) -> Dict | None:
    """Return a still-fresh cached verdict for job_id, or None.

    Entries past SCORE_CACHE_TTL_DAYS, and entries produced by the keyword
    fallback scorer rather than the AI (a transient AI outage is not a real
    rejection), are treated as misses so the job gets scored again.
    """
    entry = cache.get(job_id)
    if not entry or not entry.get("ai_scored"):
        return None
    try:
        age_days = (time.time() - float(entry["scored_at"])) / 86400
    except (KeyError, TypeError, ValueError):
        return None
    return None if age_days > SCORE_CACHE_TTL_DAYS else entry


def remember_score(cache: Dict[str, Dict], job_id: str, compat: Dict, ai_scored: bool) -> None:
    """Record a scoring verdict so overlapping title searches don't re-score it.

    Stores the raw score rather than a reject decision: min_match_score is a
    per-run setting, so a later run with a lower bar must be able to re-derive
    its own verdict from the same cached numbers.
    """
    if not job_id:
        return
    cache[job_id] = {
        "match_score": compat.get("match_score", 0.0),
        "reasoning": compat.get("reasoning", ""),
        "deal_breakers": compat.get("deal_breakers", []),
        "matched_skills": compat.get("matched_skills", []),
        "ai_scored": ai_scored,
        "scored_at": time.time(),
    }
