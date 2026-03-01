---
name: job-apply
description: Automated LinkedIn Easy Apply job search and application system. Use when the user wants to search for jobs and apply to positions matching their criteria. Searches LinkedIn Easy Apply jobs, scores them with Claude AI, generates tailored cover letters, fills application forms, and tracks application status. All profile data stays local on this machine — nothing is sent to external services. Use when user says things like "find and apply to jobs", "auto-apply for [job title]", "search for [position] jobs and apply", or "help me apply to multiple jobs automatically".
---

# Job Apply Skill

Automate LinkedIn Easy Apply job searching and application submission.
All data (profile, resume, application logs) is stored locally under `~/.local/share/job-apply/`.

## Setup

### 1. Install dependencies

```bash
pip install playwright anthropic
playwright install chromium
```

### 2. Create your profile

```bash
mkdir -p ~/.local/share/job-apply
cp ~/.openclaw/skills/job-apply/profile_template.json ~/.local/share/job-apply/profile.json
# Edit profile.json with your real information
```

### 3. Run (dry run first)

```bash
python ~/.openclaw/skills/job-apply/job_search_apply.py \
  --profile ~/.local/share/job-apply/profile.json \
  --title "Software Engineer" \
  --dry-run
```

## Usage from OpenClaw

Natural language prompts:
- "Find remote SRE jobs and apply to the best matches"
- "Auto-apply to senior DevOps engineer roles"
- "Search for Platform Engineer jobs, dry run only"
- "Apply to up to 5 Staff Engineer jobs"

**IMPORTANT for agents:** When the user asks to search/apply without specifying a particular job title, run the script with NO `--title` flag. This uses all titles from `search_criteria.job_titles` in the profile (currently 7 titles), which gives much broader coverage. Only pass `--title` if the user explicitly asks to search for a specific role.

```bash
# Default: search ALL titles from profile (recommended)
python ~/.openclaw/skills/job-apply/job_search_apply.py

# Only if user asks for a specific title
python ~/.openclaw/skills/job-apply/job_search_apply.py --title "Staff SRE"
```

The agent will:
1. Load your profile from `~/.local/share/job-apply/profile.json`
2. Search LinkedIn for Easy Apply jobs matching each title in the profile
3. Score each job against your profile using Claude AI
4. Skip low-scoring jobs, deal-breakers, and already-applied positions
5. Generate a tailored cover letter for each application
6. Submit and log all results to `~/.local/share/job-apply/applications.json`

## Data storage

| Data | Location |
|------|----------|
| Your profile | `~/.local/share/job-apply/profile.json` |
| LinkedIn session | `~/.local/share/job-apply/sessions/linkedin.json` |
| Application log | `~/.local/share/job-apply/applications.json` |
| Cover letters | `~/.local/share/job-apply/cover-letters/` |

Profile data (name, skills, experience) is sent to the Anthropic API for AI scoring and form-filling. No data is sent to any other external service besides LinkedIn and Anthropic.

## Safety defaults

- `--dry-run` flag — scores and logs jobs without submitting
- `max_applications_per_day: 10` — hard cap (configurable in profile)
- `min_match_score: 0.30` — skips poor matches (configurable in profile)
- Prompt injection detection — aborts applications with suspicious form fields
- Sensitive field detection — aborts if a form requests SSN, bank info, etc.
