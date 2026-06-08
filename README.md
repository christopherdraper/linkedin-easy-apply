# job-apply

Automated job search and application across LinkedIn Easy Apply and external ATS platforms (Greenhouse, Workday, Ashby, Lever, SmartRecruiters, Paylocity, Workable, Comeet, PageUp), powered by Claude.

A three-tier queue progressively escalates work:

- **Q1 (batch)** searches LinkedIn, scores each posting against your profile, generates a tailored cover letter, and submits via Easy Apply or by navigating the external ATS form.
- **Q2 (autonomous retry)** picks up Q1 failures above a score threshold and retries them with Playwright plus Claude page analysis. Handles email verification codes (Gmail IMAP), captchas (2captcha or capsolver), and per-ATS quirks.
- **Q3 (escalation)** surfaces anything Q2 can't finish on a web dashboard with pre-computed answers, so you complete the application by hand in under a minute.

A Flask dashboard tracks market posting volumes, per-application reports, and the Q2/Q3 queues.

For day-to-day operation see [USAGE.md](USAGE.md). For internal architecture see [CLAUDE.md](CLAUDE.md).

---

## Requirements

- Python 3.9+
- A LinkedIn account
- An Anthropic API key (https://console.anthropic.com)
- Optional but recommended:
  - Gmail account with an App Password (for ATS email verification codes)
  - 2captcha or capsolver API key (for ATS captcha challenges)
  - A SOCKS5 residential proxy (only needed for SmartRecruiters / Incapsula WAF)

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/christopherdraper/linkedin-easy-apply
cd linkedin-easy-apply

# 2. Install Python dependencies
pip install -r requirements.txt  # or: pip install playwright anthropic python-docx flask

# 3. Install the Chromium build Playwright uses
playwright install chromium
```

---

## First-run walkthrough

The fastest way to onboard is to let Claude bootstrap your profile from your resume.

### Step 1. Create the runtime directory and copy the profile template

```bash
mkdir -p ~/.local/share/job-apply
cp profile_template.json ~/.local/share/job-apply/profile.json
```

### Step 2. Have Claude fill out your profile from your resume

Open Claude Code in the repo directory. Drop in your resume (PDF or .docx) and give it this prompt:

> Read my resume at `~/Documents/resume.pdf` and any past job application questionnaires I have saved. Then fully populate `~/.local/share/job-apply/profile.json` using `profile_template.json` as the structure. Fill in `personal`, `work_authorization`, `experience`, `skills`, `screening_answers`, and `search_criteria.job_titles` based on what you can infer from my resume. For `screening_answers`, prepopulate the common categories: years of experience per major skill, work authorization, visa sponsorship, expected start date, salary expectation, willingness to relocate, security clearance, and remote vs hybrid preference. Leave `gmail_app_password`, `captcha_api_key`, and `proxy_rules` empty so I can fill them in manually. Set `documents.resume_path` to the absolute path of my resume.

Claude will produce a complete `profile.json`. Skim it once before continuing. The profile is the single source of truth for every form fill, so accurate `screening_answers` keys matter more than anything else.

### Step 3. Set your environment variables

See the [Environment variable checklist](#environment-variable-checklist) below.

### Step 4. Capture a LinkedIn session

LinkedIn ties session cookies to the IP address where you logged in, so log in from the same machine that runs the script. Run this once:

```bash
python -c "
from playwright.sync_api import sync_playwright
from pathlib import Path
session_file = Path.home() / '.local/share/job-apply/sessions/linkedin.json'
session_file.parent.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto('https://www.linkedin.com/login')
    input('Log in in the browser, then press Enter...')
    context.storage_state(path=str(session_file))
    browser.close()
"
```

If you are running on a headless server, install Xvfb plus a VNC server, point `DISPLAY=:99` at the virtual display, and run the same snippet through VNC. Sessions expire every few weeks. Re-run when the script reports an expired session.

### Step 5. Do a dry run

Submits nothing. Confirms scoring, cover-letter generation, and form discovery work.

```bash
python job_search_apply.py --title "Senior DevOps Engineer" --dry-run
```

Check the output. You should see jobs found, AI scoring, cover letters generated to `~/.local/share/job-apply/cover-letters/`, and decisions logged to `~/.local/share/job-apply/applications.json`.

### Step 6. First live batch

Start small. Five applications, conservative threshold.

```bash
python job_search_apply.py --max-applications 5 --min-score 0.75
```

### Step 7. Start the dashboard

```bash
python dashboard.py  # http://localhost:5050
```

The dashboard shows market posting volumes, application history with match scores, per-application audit pages, Q2/Q3 queues, cost stats, and interview tracking. Full reference in [DASHBOARD.md](DASHBOARD.md).

### Step 8. Process the Q2 queue

Q1 failures above the 0.70 match threshold get auto-queued to Q2. Process them with the autonomous retry agent:

```bash
python assisted_apply_mcp.py --max 10
```

That is the full daily cycle.

---

## Environment variable checklist

The only required environment variable is the Anthropic key. Everything else lives inside `profile.json` so it is one source of truth.

| Variable | Required | Purpose |
|----------|----------|---------|
| `ANTHROPIC_API_KEY` | yes | AI scoring, form fills, cover letters |

```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.bashrc
source ~/.bashrc
```

### Profile-based credentials

These live in `profile.json` under `application_settings`, not as env vars.

| Field | Required for | How to get it |
|-------|-------------|---------------|
| `gmail_app_password` | Greenhouse and PageUp email verification codes | Google Account, Security, 2-Step Verification, App Passwords. Generate one for "Mail". 16 characters, no spaces. |
| `captcha_api_key` | Lever (hCaptcha), Eightfold (reCAPTCHA), some Workdays | Sign up at 2captcha.com or capsolver.com. Add credits. |
| `captcha_service` | as above | `"2captcha"` or `"capsolver"`. Defaults to `2captcha`. |
| `proxy_rules` | SmartRecruiters (Incapsula WAF). Optional everywhere else. | Dict of `domain: socks5://host:port`. Example: `{"smartrecruiters.com": "socks5://127.0.0.1:1080"}`. |
| `auto_create_accounts` | Workday accounts | Boolean. When true the bot creates ATS accounts and stores credentials locally. |
| `max_applications_per_day` | always | Hard daily cap. Default 10. Override per-run with `--max-applications`. |
| `min_match_score` | always | Floor for the AI match score (0.0 to 1.0). Default 0.30 in code, 0.75 in the profile template. Override per-run with `--min-score`. |

Example block in `profile.json`:

```json
"application_settings": {
  "max_applications_per_day": 50,
  "min_match_score": 0.75,
  "gmail_app_password": "xxxxxxxxxxxxxxxx",
  "captcha_api_key": "abcd1234...",
  "captcha_service": "2captcha",
  "auto_create_accounts": true,
  "proxy_rules": {
    "smartrecruiters.com": "socks5://127.0.0.1:1080"
  }
}
```

---

## Running

```bash
# Always dry-run first
python job_search_apply.py --title "Senior SRE" --dry-run

# Live batch across every title in your profile
python job_search_apply.py --max-applications 50 --min-score 0.75

# Market snapshot (counts postings per title, no applications)
python job_search_apply.py --market-snapshot

# Single external URL (bypasses LinkedIn)
python job_search_apply.py --external-url https://boards.greenhouse.io/company/jobs/123

# Q2 autonomous retry
python assisted_apply_mcp.py --max 20
python assisted_apply_mcp.py --list           # show queue
python assisted_apply_mcp.py --job-id li_abc  # retry a specific entry

# Dashboard
python dashboard.py                            # http://localhost:5050
```

### Common flags (Q1)

| Flag | Default | Description |
|------|---------|-------------|
| `--profile` | `~/.local/share/job-apply/profile.json` | Profile path |
| `--title` | all titles in profile | Search a single job title |
| `--max-applications` | from profile | Cap submissions this run |
| `--min-score` | from profile | Floor for match score |
| `--dry-run` | false | Score and log only |
| `--external-url` | none | Apply to a single non-LinkedIn URL |
| `--market-snapshot` | false | Count postings per title, do not apply |
| `--source` | `linkedin` | `linkedin`, `remoteok`, `hn`, `biotech`, `all` |

---

## Data storage

All data is local. Profile content goes to the Anthropic API for scoring and form fills. Nothing else leaves the machine.

| Data | Location |
|------|----------|
| Profile | `~/.local/share/job-apply/profile.json` |
| LinkedIn session | `~/.local/share/job-apply/sessions/linkedin.json` |
| Application log | `~/.local/share/job-apply/applications.json` |
| Q2/Q3 queue | `~/.local/share/job-apply/deep_apply_queue.json` |
| Market snapshots | `~/.local/share/job-apply/search_log.json` |
| Cover letters | `~/.local/share/job-apply/cover-letters/` |
| Failed-form debug dumps | `~/.local/share/job-apply/debug/` |

---

## Safety features

- **Dry-run flag** lets you test the full pipeline without submitting.
- **Sensitive field abort** halts immediately if a form asks for SSN, bank account, passport, or similar.
- **Prompt-injection detection** scans job descriptions and form labels before passing them to the AI.
- **AI response validation** caps generated answers at sensible lengths and screens for injection.
- **Already-applied tracking** dedupes by URL via `applications.json`.
- **Per-day cap** prevents runaway batches.

---

## Where to go next

- [USAGE.md](USAGE.md) covers the daily workflow, per-ATS notes, queue maintenance, and troubleshooting.
- [DASHBOARD.md](DASHBOARD.md) is the full reference for the web dashboard (every section, every route, data sources, interview tracking).
- [CLAUDE.md](CLAUDE.md) is the developer reference: architecture, handler registry, lessons learned, and contribution guidelines.
