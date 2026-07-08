#!/usr/bin/env python3
"""
Q2 Assisted Apply agent.

Uses Playwright (sync API) with Claude AI to autonomously complete job
applications that failed during Q1 automated batch runs.  Produces a
per-application decision log for human review.

Replaces the former deep_apply_computer_use.py (Xvfb + xdotool + screenshot vision).

Usage:
    python assisted_apply_mcp.py                     # process next pending Q2
    python assisted_apply_mcp.py --job-id li_abc      # process specific job
    python assisted_apply_mcp.py --list               # list Q2 pending
    python assisted_apply_mcp.py --max 5              # process up to 5 pending
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from q2apply.analysis import (
    _PAGE_ANALYSIS_SYSTEM,
    _ai_analyze_page,
    _ai_answer_field,
    _build_profile_context,
    _get_page_text_snapshot,
)
from q2apply.cli import main
from q2apply.config import (
    DATA_DIR,
    DEBUG_DIR,
    MAX_PAGE_ATTEMPTS,
    MAX_TOTAL_STEPS,
    MODEL,
    PROFILE_PATH,
)
from q2apply.detection import (
    _detect_email_verification,
    _detect_rejection,
    _detect_submission_success,
)
from q2apply.fields import (
    _FIELD_MAP,
    _US_STATES,
    _click_element,
    _fill_field,
    _fill_pageup_combobox,
    _fill_react_select,
    _match_field_to_profile,
    _robust_click,
    _upload_resume,
)
from q2apply.logger import DecisionLogger
from q2apply.loop import (
    _clear_domain_cookies,
    _execute_action_plan,
    _run_page_loop,
    process_application,
)
from q2apply.navigation import (
    _auto_upload_files,
    _find_submit_button,
    _handle_captcha,
    _handle_login_wall,
    _navigate_linkedin_to_ats,
    _try_start_application_button,
)
from q2apply.queue_runner import (
    _get_q2_pending,
    process_by_id,
    process_next_pending,
)
from q2apply.recovery import (
    _clear_errored_uploads,
    _dismiss_cookie_banner,
    _fix_corrupted_fields,
    _handle_empty_page,
    _handle_no_actions,
    _retry_skipped_with_ai,
    _save_debug_snapshot,
)
from q2apply.verification import (
    _await_verification_result,
    _find_code_input,
    _find_otp_button,
    _handle_email_verification,
    _handle_identity_verification,
)

# Facade re-exports: external consumers (tests/, docs) import these names from
# assisted_apply_mcp. Listing them in __all__ marks them as intentionally
# exported for vulture and readers.
__all__ = [
    "DecisionLogger",
    "DATA_DIR",
    "DEBUG_DIR",
    "MAX_PAGE_ATTEMPTS",
    "MAX_TOTAL_STEPS",
    "MODEL",
    "PROFILE_PATH",
    "_FIELD_MAP",
    "_PAGE_ANALYSIS_SYSTEM",
    "_US_STATES",
    "_ai_analyze_page",
    "_ai_answer_field",
    "_auto_upload_files",
    "_await_verification_result",
    "_build_profile_context",
    "_clear_domain_cookies",
    "_clear_errored_uploads",
    "_click_element",
    "_detect_email_verification",
    "_detect_rejection",
    "_detect_submission_success",
    "_dismiss_cookie_banner",
    "_execute_action_plan",
    "_fill_field",
    "_fill_pageup_combobox",
    "_fill_react_select",
    "_find_code_input",
    "_find_otp_button",
    "_find_submit_button",
    "_fix_corrupted_fields",
    "_get_page_text_snapshot",
    "_get_q2_pending",
    "_handle_captcha",
    "_handle_email_verification",
    "_handle_empty_page",
    "_handle_identity_verification",
    "_handle_login_wall",
    "_handle_no_actions",
    "_match_field_to_profile",
    "_navigate_linkedin_to_ats",
    "_retry_skipped_with_ai",
    "_robust_click",
    "_run_page_loop",
    "_save_debug_snapshot",
    "_try_start_application_button",
    "_upload_resume",
    "main",
    "process_application",
    "process_by_id",
    "process_next_pending",
]

log = logging.getLogger("assisted_apply")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

if __name__ == "__main__":
    main()
