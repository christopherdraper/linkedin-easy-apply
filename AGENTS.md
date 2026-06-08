# AGENTS.md

Setup playbook for Claude Code (or any agentic coding assistant) deploying this repo for a new user.

When a user asks you to set up `job-apply`, run through these steps in order. Each step has a `RUN`, an `ASK USER`, or both. After each step, verify the listed condition before moving on. If verification fails, surface the actual error and stop. Do not invent fixes.

Do not skip the LinkedIn or profile bootstrap steps. They are the parts that make this useful.

---

## Step 0. Confirm you are in the right place

`RUN`:
```bash
pwd && ls README.md CLAUDE.md profile_template.json job_search_apply.py
```

`VERIFY`: All four files exist. If not, the user is not inside the repo. Stop and tell them to `cd` in.

---

## Step 1. Prerequisites

`RUN`:
```bash
python3 --version && which git && which pip
```

`VERIFY`: Python 3.9 or newer, git present, pip present. If Python is too old, stop and tell the user to upgrade. Do not try to install a new Python.

---

## Step 2. Install dependencies

`RUN`:
```bash
pip install --user playwright anthropic python-docx flask
python3 -m playwright install chromium
```

`VERIFY`: Last command completes without error. `python3 -c "import playwright, anthropic, docx, flask"` exits 0.

---

## Step 3. Anthropic API key

`ASK USER`: "Do you have an Anthropic API key? If not, get one at https://console.anthropic.com (free tier works). Paste it here, or say 'skip' to set it later."

If they paste a key (starts with `sk-ant-`):

`RUN`:
```bash
grep -q "ANTHROPIC_API_KEY" ~/.bashrc || echo 'export ANTHROPIC_API_KEY="<key>"' >> ~/.bashrc
```

Then tell them to `source ~/.bashrc` or restart their shell before the next live run.

If they skip, note it. The dry-run in step 9 will fail without a key, but everything else still works.

**Important**: Never echo the key back to the chat. Treat it like a password.

---

## Step 4. Create runtime directory

`RUN`:
```bash
mkdir -p ~/.local/share/job-apply/sessions ~/.local/share/job-apply/cover-letters ~/.local/share/job-apply/debug
cp profile_template.json ~/.local/share/job-apply/profile.json
```

`VERIFY`: `~/.local/share/job-apply/profile.json` exists.

---

## Step 5. Bootstrap profile.json from the resume

`ASK USER`: "What is the absolute path to your resume? (PDF or .docx). Also, do you have any past job-application questionnaires or screening-question answers saved anywhere? If so, share those paths too."

Once they give you the resume path:

1. Read the resume (use the Read tool for PDF/.docx).
2. Read `profile_template.json` to see the expected structure.
3. Fully populate `~/.local/share/job-apply/profile.json` with:
   - `personal`: name, email, phone, city/state/zip, LinkedIn, GitHub
   - `work_authorization`: ask the user if not obvious from the resume
   - `experience`: years_total, current_title, current_employer, specializations
   - `skills`: programming_languages, frameworks, tools (extracted from the resume)
   - `documents.resume_path`: the absolute path you were given
   - `screening_answers`: prepopulate at least these common categories with values inferred from the resume or sensible defaults:
     - years of experience per major skill they list
     - work authorization (yes/no)
     - visa sponsorship requirement (yes/no)
     - expected start date (default: "2 weeks notice")
     - salary expectation (ASK USER if not in resume)
     - willingness to relocate (ASK USER)
     - security clearance (default: "None" unless resume says otherwise)
     - remote vs hybrid preference (ASK USER)
   - `search_criteria.job_titles`: see the dedicated subsection below
   - `search_criteria.keywords_excluded`: sensible defaults (`["junior", "entry level", "intern"]` unless resume suggests otherwise)
   - `search_criteria.company_blacklist`: empty list

### Step 5a. Job titles (do not skip)

`search_criteria.job_titles` is the most important field in the whole profile. It drives both the Q1 application searches and every market snapshot. Get this wrong and the bot searches for the wrong jobs forever.

Do not just copy whatever the resume's most recent title says. Real searches cover variations and adjacent roles.

`RUN`: Based on the resume, propose 5 to 10 titles that:
- Cover the seniority bands the user is open to (one level down, current, one level up).
- Cover obvious naming variations the same role goes by at different companies. Example for a mechanical engineer: `["Mechanical Engineer", "Senior Mechanical Engineer", "Mechanical Design Engineer", "Product Design Engineer", "R&D Engineer", "Manufacturing Engineer", "Mechanical Engineering Manager"]`. Example for an SRE: `["Site Reliability Engineer", "Senior SRE", "Staff SRE", "Platform Engineer", "DevOps Engineer", "Infrastructure Engineer"]`.
- Are domain-correct. A mechanical engineer should not get "Software Engineer" suggestions. A backend engineer should not get "QA Engineer". Look at the resume's actual experience.

`ASK USER`: "These are the job titles I would track for both LinkedIn searches and market snapshots. Are these right? Add, remove, or replace any. Aim for 5 to 10 total."

Then list the proposed titles. Wait for confirmation. Edit the JSON.

If the user is clearly a domain you have no examples for (geologist, paralegal, pastry chef), do not invent titles. Ask them what they would search for if they were doing it by hand, then refine from there.

### Continuing Step 5

Leave these fields blank or null for now (step 7 will offer to fill them):
- `application_settings.gmail_app_password`
- `application_settings.captcha_api_key`
- `application_settings.proxy_rules`

`VERIFY`: Show the user a summary of what you filled in (without dumping the whole JSON). Ask them to confirm key facts: name, email, current title, top 3 skills, job titles to search. If they correct anything, edit the JSON and confirm again.

---

## Step 6. Capture a LinkedIn session

`ASK USER`: "I need to capture a LinkedIn session. This requires you to log in manually in a browser window I open. Ready? (Note: you must run the script from the same machine and IP you normally browse from, or LinkedIn will flag the session.)"

When they say yes:

`RUN` (in foreground so the user sees the browser):
```bash
python3 -c "
from playwright.sync_api import sync_playwright
from pathlib import Path
session_file = Path.home() / '.local/share/job-apply/sessions/linkedin.json'
with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto('https://www.linkedin.com/login')
    input('Log in in the browser, then press Enter here...')
    context.storage_state(path=str(session_file))
    browser.close()
    print('Session saved')
"
```

`VERIFY`: `~/.local/share/job-apply/sessions/linkedin.json` exists and is more than 1 KB.

If the user is on a headless server (no display), stop and point them to the headless instructions in `README.md` Step 4 instead of trying to work around it yourself.

---

## Step 7. Optional credentials

`ASK USER` (one at a time, with the explanation):

1. **Gmail App Password** for ATS email verification codes (used by Greenhouse, PageUp, and others).
   "Greenhouse and PageUp send email codes during application. To handle them automatically, I need a Gmail App Password. Go to Google Account, Security, 2-Step Verification, App Passwords, generate one for 'Mail'. Paste it here, or say 'skip' to handle codes manually."

2. **Captcha API key** for ATS captcha challenges.
   "Some ATS platforms (Lever, Eightfold, some Workdays) use captchas. To solve them automatically, sign up at 2captcha.com or capsolver.com and add a few dollars of credit. Paste the API key here, or say 'skip' to fail on captcha challenges."

For each value the user provides, edit `~/.local/share/job-apply/profile.json` under `application_settings` to set:
- `gmail_app_password: "<value>"`
- `captcha_api_key: "<value>"` and `captcha_service: "2captcha"` (or `"capsolver"`)

`VERIFY`: After editing, `python3 -c "import json; print(list(json.load(open('$HOME/.local/share/job-apply/profile.json'))['profile']['application_settings'].keys()))"` includes the keys you set.

**Never echo these values back.**

---

## Step 8. Set a sensible match-score floor

`RUN`: Edit `~/.local/share/job-apply/profile.json`, set `application_settings.min_match_score` to `0.75` and `application_settings.max_applications_per_day` to `10`. The template defaults are too low for a first run.

`VERIFY`: Read the two values back.

---

## Step 9. Dry run

`RUN`:
```bash
python3 job_search_apply.py --max-applications 3 --dry-run
```

`VERIFY`: Script completes without an exception. Output mentions jobs found, scored, and at least one cover letter generated. No live applications submitted.

If it fails with `ANTHROPIC_API_KEY` missing, the user skipped step 3. Stop and tell them to set it.

If it fails with `Session expired`, the LinkedIn capture in step 6 did not work. Go back to step 6.

If it fails for any other reason, surface the actual error and stop.

---

## Step 10. First small live run (offer, do not auto-run)

`ASK USER`: "Dry run succeeded. Want me to do a small live run (5 applications, score threshold 0.75) so you can verify end-to-end? Or stop here and let you take over?"

If they say yes:
`RUN`:
```bash
python3 job_search_apply.py --max-applications 5 --min-score 0.75
```

`VERIFY`: Output shows submissions or rational skips (low score, already applied). Check `~/.local/share/job-apply/applications.json` is no longer empty.

---

## Step 10a. Seed the market snapshot

`ASK USER`: "Want me to run an initial market snapshot? It counts how many open postings match each of your `job_titles` on LinkedIn and seeds the dashboard chart. Takes a few minutes. Doesn't submit anything."

If yes:
`RUN`:
```bash
python3 job_search_apply.py --market-snapshot
```

`VERIFY`: `~/.local/share/job-apply/search_log.json` exists and contains an entry with today's date. Each title in their profile has a count (a number or `null` if the LinkedIn selector did not resolve).

If most titles return `null`, the LinkedIn UI likely changed or the session expired. Tell the user and stop. Do not retry blindly.

If counts look extremely low (every title under 10), the titles in their profile may be too narrow. Suggest revisiting Step 5a with broader phrasing.

---

## Step 11. Hand off

Tell the user:
- The dashboard is at `python3 dashboard.py` -> http://localhost:5050. See [DASHBOARD.md](DASHBOARD.md).
- Day-to-day workflow is in [USAGE.md](USAGE.md). The typical cycle is: market snapshot, Q1 batch, Q2 retry, dashboard review.
- Their profile is at `~/.local/share/job-apply/profile.json`. It is the source of truth for every form fill, so improving `screening_answers` over time will improve success rates.
- LinkedIn sessions expire every few weeks. When they do, re-run step 6.

Do not start the dashboard for them. Let them decide when.

---

## What you should NOT do

- Do not skip the dry run.
- Do not commit anything to git on the user's behalf during setup.
- Do not send anything to the cloud beyond what the user explicitly approves.
- Do not echo API keys, passwords, or session tokens back to the chat after they are saved.
- Do not auto-create a 2captcha or Anthropic account for the user. Those need their payment info.
- Do not modify `~/.bashrc` for anything other than `ANTHROPIC_API_KEY`.
- Do not invent fixes when a step fails. Surface the error and stop. Most setup failures are environmental (no display, expired key, wrong Python version) and the user needs to know what they are.
