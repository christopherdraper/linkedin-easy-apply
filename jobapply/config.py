"""Path and connection constants shared across the jobapply modules."""

import os
from pathlib import Path

DATA_DIR = Path.home() / ".local" / "share" / "job-apply"
LOG_FILE = DATA_DIR / "applications.json"
SEARCH_LOG_FILE = DATA_DIR / "search_log.json"
COVER_LETTER_DIR = DATA_DIR / "cover-letters"
SESSION_FILE = DATA_DIR / "sessions" / "linkedin.json"
CREDENTIALS_FILE = DATA_DIR / "credentials.json"
ATS_ACCOUNTS_FILE = DATA_DIR / "ats_accounts.json"
# Each tenant on a shared host runs its own automation Chrome; point at it
# with JOBAPPLY_CDP_URL so one account never attaches to another's browser.
CDP_URL = os.environ.get("JOBAPPLY_CDP_URL", "http://localhost:9222")
DEBUG_DIR = DATA_DIR / "debug"
DEEP_APPLY_QUEUE_FILE = DATA_DIR / "deep_apply_queue.json"
SCORE_CACHE_FILE = DATA_DIR / "score_cache.json"
# Written when LinkedIn restricts the account; LinkedIn batches refuse to start
# until LINKEDIN_BLOCK_COOLDOWN_H hours have passed or the file is deleted.
LINKEDIN_BLOCK_FILE = DATA_DIR / "linkedin_blocked.json"
LINKEDIN_BLOCK_COOLDOWN_H = 72
