# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Quick Reference

```bash
# Lint & format
ruff check --fix .
ruff format .

# Tests
pytest                                  # full suite
pytest tests/test_screening.py          # single file
pytest tests/test_screening.py -k test_yes_no  # single test by name
pytest -v --cov=. --cov-report=term-missing     # with coverage

# Security scan
bandit -r job_search_apply.py -c pyproject.toml

# Dead code check
vulture job_search_apply.py --min-confidence 80

# Run the main script
python job_search_apply.py --title "Staff SRE" --dry-run
python job_search_apply.py --max-applications 50 --min-score 0.5

# Dashboard
python dashboard.py                     # http://localhost:5050
```

## Key CLI Flags (job_search_apply.py)

`--dry-run`, `--max-applications N`, `--min-score X`, `--title "..."`,
`--source linkedin|remoteok|hn|biotech|all`, `--market-snapshot`, `--external-url URL`

## Architecture

### Modules

| Module | Lines | Purpose |
|--------|------:|---------|
| `job_search_apply.py` | ~7400 | Core: search, score, Easy Apply + external ATS form filling, cover letters, hiring manager messaging |
| `assisted_apply_mcp.py` | ~2000 | Q2 agent: retries failed applications using Playwright + Claude AI (replaces deep_apply) |
| `deep_apply_computer_use.py` | ~500 | **Deprecated.** Q1 fallback agent using Xvfb + xdotool + screenshot vision |
| `batch_analysis.py` | ~400 | Post-batch failure analysis → creates GitHub issues for recurring patterns |
| `dashboard.py` | ~400 | Flask dashboard (port 5050): market pulse, application table, per-app report pages |

### job_search_apply.py sections

- `ApplicantProfile` dataclass — profile loading
- `_score_job` — job/profile matching via Claude AI (returns score + reasoning)
- `_navigate_form` / `submit_easy_apply` — Easy Apply modal navigation
- `_navigate_external_form` / `submit_external_apply` — external ATS (Workday, Greenhouse, Lever)
- `_ai_answer_question` / `_build_form_prompt` — AI-powered form field answers
- `_ai_draft_hiring_message` / `_send_hiring_manager_message` — post-apply DMs
- `auto_apply_workflow` / `main` — batch orchestration

### Data flow

Profile (`~/.local/share/job-apply/profile.json`) → screening answer lookup (fuzzy keyword match on form labels) → Claude AI fallback for unmatched questions → application log (`applications.json`) → dashboard reads log for display.

## CI Pipeline (GitHub Actions)

Three workflows run on push/PR to `main`:

1. **Tests** (`test.yml`): `pytest` with coverage — fails below 15% coverage
2. **Lint** (`lint.yml`): `ruff check` + `ruff format --check` + `bandit` security scan + `vulture` dead code
3. **Quality** (`quality.yml`): `radon` complexity (fails if >3 F-grade functions) + `pylint` (fails below 5.0/10)

## Pre-commit Hooks

Defined in `.pre-commit-config.yaml`: ruff lint+format, bandit, vulture, pytest.
If ruff auto-fixes files, re-stage them before committing.

## Code Style Notes

- ruff config in `pyproject.toml`: line-length 100, Python 3.9 target, double quotes
- Max cyclomatic complexity 15 (`C901`). Many complex state-machine functions carry `# noqa: C901` — add new exemptions sparingly.
- Bandit skips: `B110/B112` (intentional try/except/pass), `B310` (urlopen on API URLs), `B311` (random for timing jitter), `B404/B603/B607` (Xvfb subprocess with hardcoded args)

## AI Usage

- All AI calls use the Anthropic API via `anthropic` SDK. Key: `ANTHROPIC_API_KEY` env var.
- `_ai_answer_question`: form answers, `max_tokens=25`, retries once at 15 if >100 chars
- `_score_job`: job/profile matching, returns score + reasoning
- `_ai_draft_hiring_message`: hiring manager DMs, `max_tokens=200`
- `assisted_apply_mcp.py` uses `claude-sonnet-4-6` for page-level form reasoning

## Common Failure Modes

- **"no Apply button found"**: External listing has no detectable apply link on LinkedIn page
- **Autocomplete location fields**: Greenhouse/Lever need type-and-select, standard fill fails
- **CAPTCHA/security checks**: Unsolvable by bot, correctly aborts
- **Form stalls**: Validation errors bot can't resolve; stall recovery fills empty required fields and retries up to 4 times, then dumps debug screenshot

## Profile (profile.json)

Single source of truth. Key fields:
- `screening_answers`: question keywords → answers (fuzzy-matched against form labels)
- `search_criteria.job_titles`: titles to search (cycled in batch runs)
- `application_settings.message_hiring_manager`: send DM to poster after apply
- `resume_path`: PDF resume path for uploads

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
