# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Layout

```
~/.openclaw/skills/job-apply/          # Code lives here
  job_search_apply.py                  # Q1: search, score, Easy Apply + external ATS (~7400 lines)
  assisted_apply_mcp.py                # Q2: MCP Playwright autonomous retry agent (~2000 lines)
  deep_apply_computer_use.py           # Deprecated Q1 fallback (Xvfb + xdotool + screenshot vision)
  batch_analysis.py                    # Post-batch failure analysis -> GitHub issues for recurring patterns
  dashboard.py                         # Flask dashboard + per-application report pages
  templates/                           # dashboard.html, report.html, decision_log.html
  ats_handlers/                        # Per-ATS handler modules (Workday, Greenhouse, SmartRecruiters, etc.)
  tests/                               # pytest suite (scoring, screening, injection defense, handlers)
  pyproject.toml                       # ruff, pytest, bandit config

~/.local/share/job-apply/              # Runtime data (not in repo)
  profile.json                         # Applicant profile -- source of truth for all form fills
  applications.json                    # Application log (read by dashboard)
  deep_apply_queue.json                # Q2/Q3 retry queue
  search_log.json                      # Market snapshot data (line graph on dashboard)
  linkedin_cookies.json                # Auth session
  cover-letters/                       # Generated cover letters (.docx, legacy .txt auto-converted)
  debug/                               # Screenshots + HTML dumps of failed forms
  sessions/                            # Playwright session storage
```

## Quick Reference

```bash
# Lint & format
ruff check --fix .
ruff format .

# Tests
pytest                                           # full suite
pytest tests/test_screening.py                   # single file
pytest tests/test_screening.py -k test_yes_no    # single test by name
pytest -v --cov=. --cov-report=term-missing      # with coverage

# Security + dead-code scans
bandit -r job_search_apply.py -c pyproject.toml
vulture job_search_apply.py --min-confidence 80

# Run the main script
python job_search_apply.py --max-applications 50 --min-score 0.75
python job_search_apply.py --market-snapshot
python job_search_apply.py --external-url <url> --dry-run

# Dashboard
python dashboard.py                              # http://localhost:5050
```

## Three-Tier Queue System

- **Q1** (batch bot): `job_search_apply.py` -- searches LinkedIn, scores jobs via AI, applies via Easy Apply or external ATS navigation. Failures above 0.7 match score auto-queue to Q2.
- **Q2** (autonomous retry): `assisted_apply_mcp.py` -- uses Playwright to fill forms autonomously with AI page analysis. Navigates directly to ATS URL when available (bypassing LinkedIn). Escalates to Q3 after repeated failures.
- **Q3** (human escalation): Dashboard section for applications Q2 can't handle. Shows pre-computed prompts for manual completion.

## Job Flow: Q1 -> Q2 -> Q3

```
Q1 batch apply
  |-- submitted -----> applications.json (done)
  |-- failed (score >= 0.7, eligible category) --> deep_apply_queue.json [q2, pending]
  |-- failed (score < 0.7 or ineligible) -------> applications.json (dead end)

Q2 autonomous retry
  |-- submitted -----> applications.json updated, queue entry marked done
  |-- failed (attempts < 2) --> queue entry reset to pending for another Q2 try
  |-- failed (attempts >= 2) --> escalated to Q3 [q3, pending]

Q3 human escalation
  |-- shown on dashboard with pre-computed prompts for manual completion
```

**Q2-eligible failure categories**: `form_stuck`, `validation_error`, `captcha`, `no_apply_button`, `login_wall`, `modal_lost`, `max_steps`, `timeout`, `unknown_error`

**Q2 escalation**: After 2 failed Q2 attempts, entry moves to Q3. Within a single Q2 attempt: 4 retries on the same page state (`MAX_PAGE_ATTEMPTS`) or 30 total steps (`MAX_TOTAL_STEPS`) triggers failure.

**Queue entry schema** (`deep_apply_queue.json`):
```json
{
  "job_id": "li_abc123",
  "title": "Senior SRE",
  "company": "Acme",
  "url": "https://linkedin.com/jobs/view/...",
  "ats_url": "https://boards.greenhouse.io/...",
  "match_score": 0.92,
  "failure_reason": "form_stuck",
  "status": "pending",
  "queue": "q2",
  "q2_attempts": 0,
  "queued_at": "2026-04-14 16:00:00",
  "pre_computed": {"cover_letter_path": "...", "field_answers": {}, "scoring_reasoning": "..."},
  "decision_log": []
}
```

**Typical run cycle**:
1. `python job_search_apply.py --market-snapshot` -- check posting volumes
2. `python job_search_apply.py --max-applications 50` -- Q1 batch
3. Purge stale Q2/Q3 entries (>7 days old)
4. Escalate known-bad platforms (Alignerr, Ashby, Eightfold) from Q2 to Q3
5. `python assisted_apply_mcp.py --max 20` -- Q2 retry fresh failures

## Running

```bash
# Q1 batch apply
python job_search_apply.py --max-applications 50 --min-score 0.75

# Q1 market snapshot only (no applications)
python job_search_apply.py --market-snapshot

# Q2 process next pending
python assisted_apply_mcp.py --max 10

# Q2 specific job
python assisted_apply_mcp.py --job-id li_abc123

# Q2 list pending
python assisted_apply_mcp.py --list

# Direct ATS (bypasses LinkedIn entirely)
python job_search_apply.py --external-url https://boards.greenhouse.io/company/jobs/123
```

## Key CLI Flags (Q1)

- `--dry-run` -- score and log only, no submissions
- `--max-applications N` -- cap submissions per run
- `--min-score X` -- skip jobs below this match score (0.0-1.0), currently 0.75
- `--title "Staff SRE"` -- search specific title (default: all titles from profile)
- `--source linkedin|remoteok|hn|biotech|all` -- job source
- `--market-snapshot` -- count postings per title, no applications
- `--external-url URL` -- apply to a single external ATS URL

## Architecture Principles

### Platform-specific code belongs in platform-specific modules
When fixing bugs or adding features for a specific ATS (Workday, Greenhouse, Ashby, etc.), **never patch the generic shared code path**. Create or extend a per-platform handler instead. Each patch to shared functions like `_navigate_external_form`, `_attempt_account_creation`, or `_fill_registration_form` risks breaking every other platform. If you find yourself writing `if "workday" in url` or adding a Workday-specific selector to a generic function, stop and put it in a Workday handler.

### Plan architecture before implementing structural changes
Before modifying any function that serves multiple code paths, use `feature-dev:code-architect` to design the change, then `superpowers:writing-plans` to plan implementation. Do not jump straight to coding when the change touches shared infrastructure. A 10-minute design step prevents hours of cross-platform regression debugging.

### Composition over conditionals
Prefer a handler registry pattern (detect platform, dispatch to handler) over growing if/else chains in monolithic functions. Each ATS platform has fundamentally different quirks (Workday: React SPA + account creation + cookie banners; Greenhouse: verification codes + IMAP; Ashby: spam filter). These don't belong in the same function behind conditionals.

### Test against the platform you're changing, not just unit tests
Unit tests verify code correctness, not platform behavior. After changing ATS-specific code, test with a real URL from that platform using `--external-url`. The 258-test suite doesn't catch Workday cookie banners or Greenhouse verification flow regressions.

### Nothing is "fixed" without a real-world test
A fix is not fixed until it has been exercised against the actual site that was failing. Unit tests and code-path reasoning are necessary but not sufficient. Every handler change must be confirmed with `python job_search_apply.py --external-url <url> --dry-run` (or equivalent Q2 run) against a URL representative of the failure. If the fix applies to several handlers or sites, each one needs its own live run -- do not extrapolate from one success. Report status as "verified on <url>" or "untested against <url>"; never as just "fixed." If you cannot run the live test (no URL available, auth wall, rate limit), say so explicitly and mark the fix pending verification -- do not claim success.

## ATS Handler Registry (`ats_handlers/` package)

Per-platform modules for ATS-specific quirks. Handlers override lifecycle hooks called by the generic form-filling loop.

```
ats_handlers/
  __init__.py       # Public API: get_handler(url), register()
  _base.py          # BaseATSHandler ABC -- all hook signatures + shared helpers (cookie banner dismiss)
  _registry.py      # HandlerRegistry -- maps platform names to handler singletons
  default.py        # DefaultHandler (no-op hooks, used for unknown platforms)
  workday.py        # Cookie banners, autofill popup, login wall override
  greenhouse.py     # Email verification codes, OneTrust dismissal, ?gh_jid= iframe jump
  smartrecruiters.py # /oneclick-ui/ navigation, DataDome anti-bot
  lever.py          # Passthrough (0% success, no special handling yet)
  ashby.py          # Spam filter detection on page load + after submit
```

**Lifecycle hooks (Q1):** `pre_flight`, `on_step_start`, `resolve_login_wall`, `handle_verification_code`, `on_submit_clicked`, `detect_success`

**Lifecycle hooks (Q2):** `q2_pre_flight`, `q2_resolve_login_wall`

**Adding a new ATS handler:**
1. Create `ats_handlers/<platform>.py`
2. Subclass `BaseATSHandler`, implement relevant hooks
3. Call `register("<PlatformName>", YourHandler)` at module level
4. Add `import ats_handlers.<platform>` to `ats_handlers/__init__.py`
5. Add tests to `tests/test_ats_handlers.py`
6. Verify with a real URL: `python job_search_apply.py --external-url <url> --dry-run`

## Architecture Notes

### Modules at a glance

| Module | Lines | Purpose |
|--------|------:|---------|
| `job_search_apply.py` | ~7400 | Core: search, score, Easy Apply + external ATS form filling, cover letters, hiring manager messaging |
| `assisted_apply_mcp.py` | ~2000 | Q2 agent: retries failed applications using Playwright + Claude AI |
| `deep_apply_computer_use.py` | ~500 | **Deprecated.** Q1 fallback agent using Xvfb + xdotool + screenshot vision |
| `batch_analysis.py` | ~400 | Post-batch failure analysis -> creates GitHub issues for recurring patterns |
| `dashboard.py` | ~400 | Flask dashboard (port 5050): market pulse, application table, per-app report pages |

### Q1 (`job_search_apply.py`)
- Profile loading / dataclass (`ApplicantProfile`)
- Job scoring via Claude AI (`_score_job`, `ai_score_job`)
- Easy Apply form navigation (`_navigate_form`, `submit_easy_apply`)
- External ATS form filling (`_navigate_external_form`, `submit_external_apply`)
- AI form answering (`_ai_answer_question`, `_build_form_prompt`)
- Cover letter generation (`ai_generate_cover_letter`, `_save_cover_letter_docx`)
- Cover letter auto-conversion (`_ensure_cover_letter_docx` -- converts legacy .txt to .docx on upload)
- Hiring manager messaging (`_ai_draft_hiring_message`, `_send_hiring_manager_message`)
- Deal-breaker detection
- Market snapshot (`market_snapshot`, `_extract_results_count`)
- Queue routing (`_queue_for_deep_apply`, `_deep_apply_eligible`)
- Gmail IMAP verification code fetching (`_fetch_verification_code_from_gmail`)
- ATS URL capture: `_final_ats_url` global is set during external apply and stored in both application record and queue entry

### Q2 (`assisted_apply_mcp.py`)
- `_match_field_to_profile`: deterministic profile lookup (name, email, phone, etc.)
- `_ai_analyze_page`: sends page snapshot + profile to Claude, returns fill/click/select actions
- `_ai_answer_field`: single-field AI fallback for fields deterministic matching missed
- `_execute_action_plan`: executes AI-planned actions with profile override
- `_run_page_loop`: core form-filling loop (fill, submit, detect success/failure/verification)
- `_handle_email_verification`: Greenhouse verification code flow (IMAP fetch, enter, poll for result)
- `_await_verification_result`: polls for page transition after code entry (submitted/continue/failed)
- `_handle_identity_verification`: PageUp OTP flow (click OTP button, fetch code, enter)
- `_fill_pageup_combobox`: PageUp `-edit`/`-postback` combobox pattern with US state expansion
- `DecisionLogger`: structured decision log per application (step, action, target, value, reasoning, confidence)
- Uses `ats_url` from queue entry when available (bypasses LinkedIn-to-ATS handoff)

### Dashboard (`dashboard.py`)
- Flask app on port 5050
- `/` -- main dashboard with market pulse + application table
- `/report/<job_id>` -- per-application audit page (fields filled, cover letter, match reasoning)
- Q2/Q3 sections with decision log viewer

### Data flow

Profile (`~/.local/share/job-apply/profile.json`) -> screening answer lookup (fuzzy keyword match on form labels) -> Claude AI fallback for unmatched questions -> application log (`applications.json`) -> dashboard reads log for display.

## CI Pipeline (GitHub Actions)

Three workflows run on push/PR to `main`:

1. **Tests** (`test.yml`): `pytest` with coverage -- fails below 15% coverage
2. **Lint** (`lint.yml`): `ruff check` + `ruff format --check` + `bandit` security scan + `vulture` dead code
3. **Quality** (`quality.yml`): `radon` complexity (fails if >3 F-grade functions) + `pylint` (fails below 5.0/10)

## Linting, Pre-commit Hooks, Code Style

```bash
ruff check --fix .     # lint (ruff auto-fixes on pre-commit hook)
ruff format .          # format
pytest                 # run test suite
```

- Pre-commit hooks (`.pre-commit-config.yaml`): ruff lint+format, bandit, vulture, pytest. If ruff auto-fixes files, re-stage them before committing.
- ruff config in `pyproject.toml`: line-length 100, Python 3.9 target, double quotes.
- Max cyclomatic complexity 15 (`C901`). Existing `# noqa: C901` exemptions: `_navigate_form`, `_send_hiring_manager_message`, `_run_page_loop` -- all complex state machines. Add new exemptions sparingly.
- Bandit skips: `B110/B112` (intentional try/except/pass), `B310` (urlopen on API URLs), `B311` (random for timing jitter), `B404/B603/B607` (Xvfb subprocess with hardcoded args)

## AI Usage

- All AI calls go through the Anthropic API (`anthropic` SDK). Key: `ANTHROPIC_API_KEY` env var.
- Haiku for job scoring (cost optimization), Sonnet for form fills and cover letters.
- `_ai_answer_question` (Q1): form field answers, `max_tokens=25`, retries once at 15 if >100 chars
- `_ai_analyze_page` (Q2): full page analysis, `max_tokens=2048`, returns JSON array of actions
- `_ai_answer_field` (Q2): single-field fallback
- `_ai_draft_hiring_message`: hiring manager DMs, `max_tokens=200`
- `assisted_apply_mcp.py` uses `claude-sonnet-4-6` for page-level form reasoning

## Lessons Learned / Known Issues

### ATS URL Storage (fixed 2026-04-11)
Q1 captures `_final_ats_url` after LinkedIn redirects to external ATS. This URL is stored in both the application record (`ats_url` field) and the deep apply queue entry. Q2 prefers `ats_url` over the LinkedIn URL when navigating, bypassing the broken LinkedIn-to-ATS handoff. Before this fix, Q2 retries went through LinkedIn again and hit the same failures.

### Cover Letters are .docx (fixed 2026-04-11)
Cover letters are generated as `.docx` (Calibri 11pt via python-docx). Legacy `.txt` files are auto-converted to `.docx` on upload via `_ensure_cover_letter_docx()`. This matters because some ATS platforms (e.g., Comeet) silently reject `.txt` uploads and the form won't submit. Also, .docx is better for ATS keyword extraction and AI screening.

### Screening Answer False Matches (fixed 2026-04-11)
Short screening answer keys like `"state"` would substring-match against question text containing "United States", causing "Indiana" to be filled for work authorization yes/no questions. Fix: word-boundary matching for short keys (<10 chars) + skip list for generic field names (state, city, country, language, etc.) when the label contains `?`. The AI system prompt also has explicit rules about yes/no questions.

### Generic Apply-button selector required :visible (fixed 2026-04-24)
The generic Apply-button lookup in `_navigate_external_form` used `button:has-text('Apply')` without a visibility filter. On pages with OneTrust cookie preference-center modals, this matched the hidden `#filter-apply-handler` button ("Apply" = apply cookie filter settings), which `_safe_click`'s JS fallback fired successfully but navigated nowhere, trapping the form loop in a no-op spin ("Job listing page detected" 3x then form_stuck). Fix: added `:visible` to every alternative in the Apply-button selector. Verified on Coalition's embedded Greenhouse.

### Greenhouse iframe embeds (fixed 2026-04-24)
Sites that embed Greenhouse via `?gh_jid=` on a company careers page (Coalition, Nintex, etc.) render the Greenhouse form inside an iframe. The outer page has no form fields, so the generic loop only sees an Apply button, clicking it does not navigate, and the loop stalls. `GreenhouseHandler.pre_flight` now detects the embed and navigates directly to the iframe's src URL (`job-boards.greenhouse.io/embed/job_app?for=...&token=...`), so the form loop runs against the real Greenhouse form. Verified on Coalition (40 visible inputs after jump).

### Greenhouse Verification Codes
- Codes are fetched via Gmail IMAP (`_fetch_verification_code_from_gmail`)
- Q1 uses 480s timeout (PageUp emails are slow), Q2 uses 120s
- After entering code, must poll for page transition -- Greenhouse confirmation pages have minimal DOM that looks "empty" to the snapshot parser
- `_detect_submission_success` must be checked before declaring a page empty (`_handle_empty_page`)
- Gmail app password in `profile.json` -> `application_settings.gmail_app_password`

### Market Snapshot Data Quality
- `_extract_results_count` only uses primary LinkedIn selectors (`.jobs-search-results-list__subtitle`)
- Fallback extractors (document.title, body text regex) were removed because they return unfiltered global counts, not the remote-filtered results, causing false data spikes
- If selectors break, the count returns `None` instead of a wrong number -- better to have gaps than fake data
- Clean bad data from `search_log.json` before it corrupts the line graph

### PageUp ATS
- Uses `-edit`/`-postback` combobox pattern: text input with `-edit` suffix + hidden value with `-postback` suffix
- US state codes must be expanded (IN -> Indiana) for dropdown matching
- Submit button ordering matters: "Save and continue" vs "Save and exit" -- use text-specific selectors before generic `type="submit"`
- OTP verification: emails take 2-7 minutes, use 480s timeout
- Multiple OTP logins can trigger Okta account creation, which then requires password login

### min_match_score Default (gotcha)
The code default for `min_match_score` is **0.30** (line ~7362), not 0.75. The 0.75 threshold comes from `profile.json -> application_settings.min_match_score`. If that key is ever missing or the profile fails to load that section, jobs scoring as low as 0.30 will be applied to. Always verify the profile has this key set. This caused a 0.62-scoring FICO job to be applied to before the profile key was added.

### Q2 Sequential Processing (operational)
Q2 processes entries sequentially. If it hits a known-bad platform (Alignerr OAuth, Eightfold CAPTCHA loop), it hangs and blocks remaining entries. **Best practice**: before running Q2, escalate entries on known-bad platforms to Q3 first. Check with `--list` and clean up before `--max N`.

### Queue Maintenance
- Stale queue entries (>7 days old) should be purged periodically -- expired LinkedIn listings return "promoted ad (no apply button)" or empty pages
- Entries stuck at `in_progress` from killed Q2 runs need manual reset to `pending` or escalation to Q3
- The purge command: `python3 -c "..."` against `deep_apply_queue.json` filtering by `queued_at` timestamp

### Eightfold ATS
- Uses reCAPTCHAv2 that can loop (solve one, get served another immediately)
- 2captcha integration solves them but the loop can burn credits indefinitely
- Kill the process if it's solving more than 2 CAPTCHAs for a single application

### Q2 TargetClosedError (observed 2026-04-20)
- Playwright browser page crashes mid-form with `TargetClosedError: Page.query_selector_all: Target page, context or browser has been closed`
- Hit 3 out of 10 entries in a single Q2 run (Netflix, Zscaler, ICF)
- Not platform-specific -- different ATS backends (Netflix custom, Greenhouse, ICF custom)
- Entries get another Q2 attempt on next run; escalate to Q3 after 2 total attempts
- Possible causes: resource exhaustion, page navigation race, Playwright session instability

### Job Pool Saturation (observed 2026-04-20)
- Dedup filter showing 1,455-1,467 already-applied jobs per title search
- Q1 batches yielding very few new jobs (6-7 found per title vs dozens earlier)
- Expect diminishing returns from `--max-applications 50` runs until new postings appear

### ATS Platform Success Rates (as of 2026-04-20)
- **Easy Apply**: ~67% success
- **Greenhouse**: ~57% success (verification codes are the main challenge)
- **Ashby**: ~3% success (application-level spam filter, proxy doesn't help)
- **Lever**: 0% success (all 7 attempts failed; hCaptcha the usual blocker)
- **Workday**: ~29% success (requires account, dropdown loops)
- **Eightfold**: low success (CAPTCHA loops)
- **Comeet**: works with .docx fix (only 1 application seen)

### Platforms That Don't Work
- **Alignerr**: requires OAuth social login (Google/LinkedIn), bot can't handle
- **Ashby**: spam filter blocks at application level, not IP-based
- **HRMDirect**: "START YOUR APPLICATION" button click doesn't advance (JS SPA issue)
- **Workday**: frequently requires account creation, dropdowns loop
- **Eightfold**: reCAPTCHA loops drain 2captcha credits without progress

### Generic failure categories (surface in `applications.json -> status`)
- **"no Apply button found"**: External listing has no detectable apply link on LinkedIn page
- **Autocomplete location fields**: Greenhouse/Lever need type-and-select, standard fill fails
- **CAPTCHA/security checks**: Correctly aborts when 2captcha can't solve (e.g. recaptchav2_enterprise ERROR_CAPTCHA_UNSOLVABLE)
- **Form stalls**: Validation errors bot can't resolve; stall recovery fills empty required fields and retries up to 4 times, then dumps debug screenshot

## Profile (profile.json)

The profile is the single source of truth. Key fields:
- `profile.screening_answers`: dict of 203 question keywords -> answers (matched fuzzy against form labels)
- `profile.work_authorization`: authorized_to_work_us, us_citizen, requires_visa_sponsorship
- `profile.personal.years_total`: 12
- `search_criteria.job_titles`: 15 titles searched in batch runs
- `application_settings.message_hiring_manager`: bool -- send DM to poster after apply
- `application_settings.gmail_app_password`: Gmail app password for verification code IMAP fetch
- `application_settings.auto_fetch_verification_codes`: bool toggle
- `application_settings.captcha_api_key` + `captcha_service`: 2captcha or capsolver credentials
- `profile.documents.resume_path`: path to .docx resume
- `profile.preferences.min_match_score`: 0.75
- `profile.preferences.proxy_rules`: domain -> SOCKS5 proxy mapping (only smartrecruiters.com currently)

## Operational Notes

- **Application volume**: ~1,470+ jobs applied as of 2026-04-20 across 15 titles
- **Q1 batch cadence**: `--max-applications 50` per run, but yields dropping (6-7 new jobs per title as of 2026-04-20 due to saturation)
- **Q2 after Q1**: New Q1 failures above 0.7 auto-queue to Q2. Run Q2 after each Q1 batch to catch fresh failures.
- **Market snapshots**: Run daily to track posting volume trends. Data in `search_log.json`, visualized on dashboard line graph.
- **Queue hygiene**: Purge Q2/Q3 entries older than 7 days before each Q2 run. Escalate known-bad platforms (Alignerr, Ashby, Eightfold) to Q3 before running Q2 to avoid blocking.

## Proxy
- SOCKS5 proxy on localhost:1080 for bypassing WAFs
- Currently only used for SmartRecruiters (Incapsula WAF)
- PageUp (Incapsula) was also proxied previously
- Ashby spam filter is application-level, proxy doesn't help

---

# Behavioral Guidelines

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
