# Job Apply Skill — CLAUDE.md

## Project Layout

```
~/.openclaw/skills/job-apply/     # Code lives here
  job_search_apply.py             # Main module: search, score, apply (Easy Apply + external ATS)
  dashboard.py                    # Flask dashboard + per-application report pages
  templates/                      # dashboard.html, report.html
  tests/                          # pytest suite (scoring, screening, injection defense, etc.)
  pyproject.toml                  # ruff, pytest, bandit config

~/.local/share/job-apply/         # Runtime data (not in repo)
  profile.json                    # Applicant profile — source of truth for all form fills
  applications.json               # Application log (read by dashboard)
  search_log.json                 # Market snapshot data
  linkedin_cookies.json           # Auth session
  cover-letters/                  # Generated cover letters (one per application)
  debug/                          # Screenshots + HTML dumps of failed forms
  sessions/                       # Playwright session storage
```

## Running Batches

```bash
python ~/.openclaw/skills/job-apply/job_search_apply.py \
  --max-applications 50 --min-score 0.5
```

## Key CLI Flags

- `--dry-run` — score and log only, no submissions
- `--max-applications N` — cap submissions per run
- `--min-score X` — skip jobs below this match score (0.0–1.0)
- `--title "Staff SRE"` — search specific title (default: all titles from profile)
- `--source linkedin|remoteok|hn|biotech|all` — job source
- `--market-snapshot` — count postings per title, no applications
- `--external-url URL` — apply to a single external ATS URL

## Architecture Notes

- `job_search_apply.py` is one large module (~4600 lines). Key sections:
  - Profile loading / dataclass (`ApplicantProfile`)
  - Job scoring via Claude AI (`_score_job`)
  - Easy Apply form navigation (`_navigate_form`, `submit_easy_apply`)
  - External ATS form filling (`_navigate_external_form`, `submit_external_apply`)
  - AI form answering (`_ai_answer_question`, `_build_form_prompt`)
  - Cover letter generation
  - Hiring manager messaging (`_ai_draft_hiring_message`, `_send_hiring_manager_message`)
  - Deal-breaker detection
- Dashboard (`dashboard.py`) is a separate Flask app on port 5050
  - `/` — main dashboard with market pulse + application table
  - `/report/<job_id>` — per-application audit page (fields filled, cover letter, match reasoning)

## Linting & Tests

```bash
ruff check --fix .     # lint (ruff auto-fixes on pre-commit hook)
ruff format .          # format
pytest                 # run test suite
```

- ruff enforces max complexity 15 (`C901`). Two functions have `# noqa: C901` exemptions:
  `_navigate_form` and `_send_hiring_manager_message` — both are inherently complex state machines.
- Pre-commit hook runs ruff check + format. If it modifies files, re-stage before committing.

## AI Usage

- All AI calls go through the Anthropic API (Claude Sonnet for form fills/scoring, same for cover letters)
- `_ai_answer_question`: form field answers, `max_tokens=25`, retries once at 15 if >100 chars
- `_ai_draft_hiring_message`: hiring manager DMs, `max_tokens=200`
- `_score_job`: job/profile matching, returns score + reasoning
- API key via `ANTHROPIC_API_KEY` env var

## Common Failure Modes

- **"no Apply button found"**: External job listing has no detectable apply link on LinkedIn page
- **Autocomplete location fields**: Greenhouse/Lever location inputs need type-and-select, standard fill doesn't work
- **CAPTCHA/security checks**: Unsolvable by bot, form correctly aborts
- **Form stalls**: Validation errors the bot can't resolve (e.g., missing location). Stall recovery fills empty required fields and retries up to 4 times before dumping debug screenshot.

## Profile (profile.json)

The profile is the single source of truth. Key fields:
- `screening_answers`: dict of question keywords → answers (matched fuzzy against form labels)
- `years_total`: 12
- `search_criteria.job_titles`: list of titles to search (cycled through in batch runs)
- `application_settings.message_hiring_manager`: bool — send DM to poster after apply
- `resume_path`: path to PDF resume uploaded with applications
