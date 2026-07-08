"""Path and model constants shared across the q2apply modules."""

from pathlib import Path

DATA_DIR = Path.home() / ".local" / "share" / "job-apply"
DEBUG_DIR = DATA_DIR / "debug"
PROFILE_PATH = DATA_DIR / "profile.json"

MODEL = "claude-sonnet-5"
MAX_PAGE_ATTEMPTS = 4
MAX_TOTAL_STEPS = 30
