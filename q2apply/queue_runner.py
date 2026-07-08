"""Q2 queue processing: pending selection, per-entry dispatch, escalation."""

import json
import logging
from typing import Dict, List, Optional

from job_search_apply import (
    ApplicantProfile,
    _escalate_to_q3,
    _load_deep_apply_queue,
    _mark_deep_apply_done,
    _save_deep_apply_queue,
)
from q2apply.config import PROFILE_PATH
from q2apply.loop import process_application

log = logging.getLogger(__name__)


def _require_apply_setup(profile: ApplicantProfile) -> None:
    """Fail fast before a Q2 apply run if the profile is misconfigured.

    Q2 also creates ATS accounts (navigation._handle_login_wall), so it needs
    the same Gmail App Password prerequisite as Q1.
    """
    err = profile.apply_prerequisite_error()
    if err:
        log.error("❌ %s", err)
        raise SystemExit(1)


def _get_q2_pending() -> List[Dict]:
    """Get all pending Q2 entries from the queue."""
    queue = _load_deep_apply_queue()
    return [q for q in queue if q.get("status") == "pending" and q.get("queue", "q2") == "q2"]


def process_next_pending(
    proxy: Optional[str] = None,
    max_count: int = 1,
) -> List[Dict]:
    """Process up to max_count pending Q2 entries. Returns results list."""
    profile = ApplicantProfile.from_dict(json.loads(PROFILE_PATH.read_text()))
    _require_apply_setup(profile)
    pending = _get_q2_pending()

    if not pending:
        log.info("No pending Q2 entries in queue.")
        return []

    results = []
    for entry in pending[:max_count]:
        job_id = entry["job_id"]

        # Mark as in_progress
        queue = _load_deep_apply_queue()
        for q in queue:
            if q["job_id"] == job_id:
                q["status"] = "in_progress"
                break
        _save_deep_apply_queue(queue)

        status = process_application(entry, profile, proxy=proxy)
        log.info("Result for %s: %s", job_id, status)

        if status == "submitted":
            _mark_deep_apply_done(job_id, "submitted", None)
        elif status.startswith("failed"):
            # Check if we should escalate to Q3
            queue = _load_deep_apply_queue()
            q_entry = next((q for q in queue if q["job_id"] == job_id), None)
            attempts = q_entry.get("q2_attempts", 0) if q_entry else 0

            if attempts >= 2:
                _escalate_to_q3(job_id, status.replace("failed: ", ""))
                status = f"escalated: {status}"
            else:
                # Reset to pending for another Q2 attempt
                for q in queue:
                    if q["job_id"] == job_id:
                        q["status"] = "pending"
                        break
                _save_deep_apply_queue(queue)

        results.append({"job_id": job_id, "status": status})

    return results


def process_by_id(
    job_id: str,
    proxy: Optional[str] = None,
) -> str:
    """Process a specific job by ID. Returns status string."""
    profile = ApplicantProfile.from_dict(json.loads(PROFILE_PATH.read_text()))
    _require_apply_setup(profile)
    queue = _load_deep_apply_queue()
    entry = next((q for q in queue if q["job_id"] == job_id), None)

    if not entry:
        return f"Job ID '{job_id}' not found in queue"

    # Mark as in_progress
    entry["status"] = "in_progress"
    _save_deep_apply_queue(queue)

    status = process_application(entry, profile, proxy=proxy)
    log.info("Result for %s: %s", job_id, status)

    if status == "submitted":
        _mark_deep_apply_done(job_id, "submitted", None)
    elif status.startswith("failed"):
        queue = _load_deep_apply_queue()
        q_entry = next((q for q in queue if q["job_id"] == job_id), None)
        attempts = q_entry.get("q2_attempts", 0) if q_entry else 0
        if attempts >= 2:
            _escalate_to_q3(job_id, status.replace("failed: ", ""))
        else:
            for q in queue:
                if q["job_id"] == job_id:
                    q["status"] = "pending"
                    break
            _save_deep_apply_queue(queue)

    return status
