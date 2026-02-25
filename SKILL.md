---
name: job-apply
description: Automated job search and application system. Use when the user wants to search for jobs and apply to positions matching their criteria. Handles job searching across LinkedIn, Indeed, Glassdoor, ZipRecruiter, and Wellfound, generates tailored cover letters, fills application forms, and tracks application status. All profile data stays local on this machine — nothing is sent to external services. Use when user says things like "find and apply to jobs", "auto-apply for [job title]", "search for [position] jobs and apply", or "help me apply to multiple jobs automatically".
---

# Job Apply Skill

Automate job searching and application submission across multiple platforms.
All data (profile, resume, application logs) is stored locally under `~/.local/share/job-apply/`.

## Setup

### 1. Install dependencies

```bash
pip install playwright
playwright install chromium
```

### 2. Create your profile

```bash
mkdir -p ~/.local/share/job-apply
cp ~/.openclaw/agents/main/agent/skills/job-apply/profile_template.json ~/.local/share/job-apply/profile.json
# Edit profile.json with your real information
```

### 3. Run (dry run first)

```bash
python ~/.openclaw/agents/main/agent/skills/job-apply/job_search_apply.py \
  --profile ~/.local/share/job-apply/profile.json \
  --title "Software Engineer" \
  --dry-run
```

## Usage from OpenClaw

Natural language prompts:
- "Find Python developer jobs in San Francisco"
- "Search for remote backend engineer positions and apply to the top 5 matches"
- "Auto-apply to senior software engineer roles with 100k+ salary"
- "Show me my application history"

The agent will:
1. Load your profile from `~/.local/share/job-apply/profile.json`
2. Search the specified platforms
3. Score each job against your profile
4. Generate a tailored cover letter
5. Ask for confirmation before submitting each application
6. Log all results to `~/.local/share/job-apply/applications.json`

## Data storage

| Data | Location |
|------|----------|
| Your profile | `~/.local/share/job-apply/profile.json` |
| Application log | `~/.local/share/job-apply/applications.json` |
| Cover letters | `~/.local/share/job-apply/cover-letters/` |
| Session cookies | `~/.local/share/job-apply/sessions/` |

No data leaves this machine except what is submitted directly to the job platform.

## Safety defaults

- `dry_run: true` by default — will not submit without `--no-dry-run`
- `require_confirmation: true` — asks before each application
- `max_applications_per_day: 10` — hard cap
- `min_match_score: 0.75` — skips poor matches

## Supported platforms

- LinkedIn (Easy Apply)
- Indeed
- Glassdoor
- ZipRecruiter
- Wellfound (AngelList)
