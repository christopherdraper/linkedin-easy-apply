# LinkedIn Easy Apply Automation

Automated LinkedIn job search and Easy Apply submission using Claude AI.

**What it does:**
- Searches LinkedIn for Easy Apply jobs matching your titles and criteria
- Scores each job against your profile using Claude (skips poor matches and deal-breakers)
- Generates a personalized cover letter for each application
- Fills out screening questions — explicit answers from your profile first, Claude as fallback
- Logs every application with AI-written notes so you know what you applied to if they call
- Detects and aborts applications that request sensitive data (SSN, bank info) or contain prompt injection

---

## Requirements

- Python 3.9+
- A LinkedIn account
- An Anthropic API key (free tier works — get one at https://console.anthropic.com)

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/christopherdraper/linkedin-easy-apply
cd linkedin-easy-apply

# 2. Install Python dependencies
pip install playwright anthropic

# 3. Install the Chromium browser used for automation
playwright install chromium
```

---

## LinkedIn Session Setup

The script runs a headless browser and needs an active LinkedIn session. LinkedIn
ties session cookies to the IP address where you logged in, so **you must log in
from the same machine that will run the script**.

### Option A — Desktop machine (simplest)

If you're running the script on your own laptop or desktop:

```bash
# Run this helper to open a visible browser window, log in manually, then save cookies
python - <<'EOF'
from playwright.sync_api import sync_playwright
from pathlib import Path
import json

session_file = Path.home() / ".local/share/job-apply/sessions/linkedin.json"
session_file.parent.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://www.linkedin.com/login")
    input("Log in to LinkedIn in the browser window, then press Enter here...")
    context.storage_state(path=str(session_file))
    browser.close()
    print(f"Session saved to {session_file}")
EOF
```

### Option B — Headless server

If you're running on a remote server without a display, use Xvfb + a VNC viewer:

```bash
# Install virtual display and VNC server
sudo dnf install -y xorg-x11-server-Xvfb x11vnc chromium   # Fedora/RHEL
# sudo apt install -y xvfb x11vnc chromium-browser           # Debian/Ubuntu

# Start virtual display
Xvfb :99 -screen 0 1280x800x24 &
export DISPLAY=:99

# Start VNC (bind to localhost only — tunnel via SSH or WireGuard)
x11vnc -display :99 -forever -localhost &

# Open Chromium on the virtual display
chromium-browser --no-sandbox &
```

Then connect with a VNC client (e.g. RealVNC, TigerVNC) and navigate to
`linkedin.com`, log in, then extract cookies:

```bash
python - <<'EOF'
from playwright.sync_api import sync_playwright
from pathlib import Path

session_file = Path.home() / ".local/share/job-apply/sessions/linkedin.json"
session_file.parent.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)  # set headless=False, use DISPLAY=:99
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://www.linkedin.com/login")
    input("Log in to LinkedIn, then press Enter...")
    context.storage_state(path=str(session_file))
    browser.close()
    print(f"Session saved to {session_file}")
EOF
```

> Sessions expire after a few weeks. Re-run this whenever the script reports that
> your session has expired.

---

## Profile Setup

Copy the template and fill in your information:

```bash
mkdir -p ~/.local/share/job-apply
cp profile_template.json ~/.local/share/job-apply/profile.json
```

Then edit `~/.local/share/job-apply/profile.json`. Key sections:

### `personal`
Your name, email, phone, location, and profile URLs. The city/state/zip are used
to auto-fill contact fields in application forms.

### `experience`
Your current title, employer, total years, and specializations. Specializations
are used by the AI when answering questions about related skills.

### `skills`
List your programming languages, frameworks, and tools. The AI uses this when
scoring jobs and answering "years of experience with X" questions.

### `screening_answers`
Pre-written answers for common questions. Keys are matched against form field
labels (case-insensitive substring match). For example:

```json
"screening_answers": {
  "years of experience with kubernetes": "5+",
  "years of experience with python": "9",
  "why leave current job": "Seeking new challenges",
  "do you require visa sponsorship": "No",
  "are you authorized to work in the us": "Yes"
}
```

For any question not matched here, Claude will generate an answer from your
profile context.

### `application_settings`

| Field | Default | Description |
|-------|---------|-------------|
| `max_applications_per_day` | `10` | Hard cap per run |
| `min_match_score` | `0.30` | 0.0–1.0 — skip jobs below this threshold |

### `search_criteria`

| Field | Description |
|-------|-------------|
| `job_titles` | Used as `--title` args when running via the wrapper script |
| `keywords_excluded` | Skip jobs whose title contains these strings |
| `company_blacklist` | Skip jobs from these companies (exact match, case-insensitive) |

---

## Cover Letter Template (optional)

Create a cover letter template at `~/.local/share/job-apply/cover_letter_template.txt`.

The template supports `{PLACEHOLDER}` tokens that Claude will fill in. You can also
add a section after `---\nTEMPLATE INSTRUCTIONS FOR AI:` with tone/style guidance.
Point to it in your profile:

```json
"documents": {
  "resume_path": "~/Documents/resume.pdf",
  "cover_letter_template_path": "~/.local/share/job-apply/cover_letter_template.txt"
}
```

If no template is provided, a minimal cover letter is generated automatically.

---

## Anthropic API Key

Set your API key in your shell environment:

```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.bashrc
source ~/.bashrc
```

The script works without an API key (falls back to keyword-based scoring and
template cover letters), but AI features are strongly recommended.

---

## Running

```bash
# Always do a dry run first — no applications will be submitted
python job_search_apply.py \
  --profile ~/.local/share/job-apply/profile.json \
  --title "Senior DevOps Engineer" \
  --dry-run

# Live run for one title
python job_search_apply.py \
  --profile ~/.local/share/job-apply/profile.json \
  --title "Senior Site Reliability Engineer"

# Limit to 5 applications, raise the score threshold
python job_search_apply.py \
  --title "Staff Platform Engineer" \
  --max-applications 5 \
  --min-score 0.65
```

### All flags

| Flag | Default | Description |
|------|---------|-------------|
| `--profile` | `~/.local/share/job-apply/profile.json` | Path to your profile |
| `--title` | *(required)* | Job title to search for |
| `--location` | none | City/region to filter by (leave blank for remote) |
| `--max-applications` | from profile | Max applications this run |
| `--min-score` | from profile | Minimum AI match score (0.0–1.0) |
| `--dry-run` | false | Score and log jobs without submitting |
| `--proxy` | none | HTTP/SOCKS5 proxy URL if needed |

---

## Data Storage

All data is stored locally. Profile data (name, skills, experience) is sent to the
Anthropic API for AI scoring and form-filling. No data is sent to any other
external service besides LinkedIn and Anthropic.

| Data | Location |
|------|----------|
| Your profile | `~/.local/share/job-apply/profile.json` |
| LinkedIn session | `~/.local/share/job-apply/sessions/linkedin.json` |
| Application log | `~/.local/share/job-apply/applications.json` |
| Cover letters | `~/.local/share/job-apply/cover-letters/` |

---

## Safety Features

- **Prompt injection detection** — job descriptions and form field labels are
  scanned for injection patterns before being passed to the AI
- **Sensitive field abort** — applications are immediately aborted if a form
  requests SSN, bank account numbers, passport numbers, or other data that should
  never appear in a job application
- **AI response validation** — AI-generated form answers are checked for length
  and injection before being typed into fields
- **`--dry-run` flag** — test everything without submitting
- **Already-applied tracking** — the script skips jobs you've previously applied
  to (tracked by URL in `applications.json`)
