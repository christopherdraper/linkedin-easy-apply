"""Path and connection constants shared across the jobapply modules."""

from pathlib import Path

DATA_DIR = Path.home() / ".local" / "share" / "job-apply"
LOG_FILE = DATA_DIR / "applications.json"
SEARCH_LOG_FILE = DATA_DIR / "search_log.json"
COVER_LETTER_DIR = DATA_DIR / "cover-letters"
SESSION_FILE = DATA_DIR / "sessions" / "linkedin.json"
CREDENTIALS_FILE = DATA_DIR / "credentials.json"
ATS_ACCOUNTS_FILE = DATA_DIR / "ats_accounts.json"
CDP_URL = "http://localhost:9222"
DEBUG_DIR = DATA_DIR / "debug"
DEEP_APPLY_QUEUE_FILE = DATA_DIR / "deep_apply_queue.json"
