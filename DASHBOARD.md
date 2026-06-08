# Dashboard guide

A Flask app that visualizes everything the job-apply pipeline produces: market pulse, application history, per-application audits, the Q2/Q3 retry queue, and interview tracking. Read-only for the most part, with one stateful workflow (interview tracking) you can drive from the UI.

## Running

```bash
python dashboard.py              # http://localhost:5050
python dashboard.py --port 8080  # custom port
```

The app reads runtime files under `~/.local/share/job-apply/`. Nothing it does writes to the application log or queue, except interview tracking (`interviews.json`).

## Data sources

| File | Used for |
|------|----------|
| `applications.json` | All application history, status breakdowns, score histogram, platform success rates, cost stats |
| `search_log.json` | Market pulse and history charts |
| `deep_apply_queue.json` | Q2 and Q3 queue tables, decision log |
| `interviews.json` | Interview tracking section (created when you mark the first application) |
| `cover-letters/*.docx` | Read on demand for the per-application report page |
| `profile.json` | Used to compose pre-filled prompts on the Q3 deep-apply page |

If any of these files are missing, the corresponding section either shows zero counts or is hidden.

---

## Home page (`/`)

### Job Market Pulse
Per-title posting volume rolled up three ways: total ever seen, past 7 days, past 24 hours. Sourced from `search_log.json`, which is appended every time you run `--market-snapshot`. If a row reads zero, either you have not run a snapshot for that title or the LinkedIn selector broke for that run (it returns `None` rather than a wrong number).

### Market History
Time-series chart of the same data. Useful for spotting posting cycles (Monday spikes, end-of-quarter dips) and tracking the saturation curve for any title you have been running for a while.

### Application Status
Single-row breakdown of every application by terminal state: submitted, failed, dry-run, skipped. Sourced from `applications.json -> status`.

### Success Rate by ATS Platform
Table of submitted vs failed counts per detected ATS. Populated from `applications.json -> ats_platform`. Use this when you are deciding whether to invest time in a new handler. A platform with five entries and zero submissions is screaming for attention.

### Failure Breakdown
Histogram of `applications.json -> failure_category`. Categories include `form_stuck`, `validation_error`, `captcha`, `no_apply_button`, `login_wall`, `modal_lost`, `max_steps`, `timeout`, `unknown_error`. If one category dominates, that is your next handler target.

### Match Score Distribution
Histogram of AI match scores in 10% bins. Useful for sanity-checking your scoring threshold. If most submissions land between 0.5 and 0.7 and you have `min_score 0.75` set, something is off.

### Hiring Manager Messages
Shows how many post-apply DMs were sent vs how many were eligible. Only meaningful if `application_settings.message_hiring_manager: true` in your profile.

### Cost
Running total of Anthropic API spend across all applications, plus average cost per submitted and per failed application. Failed applications are usually more expensive (they burn AI calls before the form rejects them).

### Interview Pending
Applications you have manually flagged as interview-track. See the "Interview tracking" section below.

### Assisted Apply Queue (Q2)
Pending, in-progress, done, and success counts for the autonomous retry queue. Each row links to the per-application decision log so you can see what the Q2 agent tried and why it gave up.

### Needs Human Review (Q3)
Applications Q2 could not finish. Each row links to a pre-computed prompt page (cover letter, screening answers, scoring reasoning) so you can complete the application by hand in a minute or two.

### All Applications
Searchable, sortable table of every application. Click any row to open the per-application report.

---

## Per-application report (`/report/<job_id>`)

Audit page for a single application. Shows:

- Job metadata (title, company, posting URL, ATS URL if redirected)
- AI match score and reasoning
- Every form field that got filled, with the source (profile lookup vs AI generation)
- The full generated cover letter
- Submission status and any failure reason

Use this to debug screening-answer false matches or to verify that a high-stakes application went out with the right cover letter.

---

## Q2 decision log (`/decision-log/<job_id>`)

Structured log of every action the Q2 autonomous agent took on one application: step, action type, target selector, value typed, AI reasoning, and confidence score. When a Q2 attempt fails, this is the first place to look. Patterns to watch for:

- Repeated `fill` actions on the same field — usually a stale selector or hidden duplicate input.
- Low-confidence `click` on a generic Submit button — the AI was guessing because no clear nav button existed.
- A run that stops at step 4 to 6 with no failure category — TargetClosedError from Playwright. Re-queue and try again.

---

## Q3 pre-filled prompt (`/deep-apply/<job_id>`)

Renders a one-page prompt with the cover letter, every known field answer, and the AI scoring reasoning. Copy it out, walk through the form yourself. This page is what makes Q3 worth running rather than dropping failed applications entirely.

---

## Interview tracking

The dashboard is also the simplest place to track which applications have moved to interview. Three POST endpoints, all driven from the UI:

| Action | Route | What it does |
|--------|-------|-------------|
| Mark interview-pending | `POST /interview/add/<job_id>` | Adds to `interviews.json` with a timestamp and optional notes |
| Update notes | `POST /interview/update/<job_id>` | Edits the notes field on an existing entry |
| Remove | `POST /interview/remove/<job_id>` | Drops the entry |

`interviews.json` is created the first time you add one. It is independent of `applications.json` so removing an interview does not affect the application history.

---

## Live refresh (`/api/data`)

JSON endpoint that returns the same data the home page uses (market stats, status counts, failure categories, platform stats, queue counts). Polled by the dashboard JavaScript for periodic refresh without reloading the page. You can also call it directly if you want to script alerts (`curl http://localhost:5050/api/data | jq '.q2_pending_count'`).

---

## Operational tips

- **Open it during Q1 batches** so you can watch applications land in real time. The market pulse and counts update on the next page load; the live refresh handles the rest.
- **Refresh after every Q2 run** to see which entries went through, which fell back, and which escalated to Q3. The Q3 section is the only one with a "do something" call to action.
- **The dashboard is read-only for application data.** It never writes to `applications.json` or `deep_apply_queue.json`. The only files it writes are `interviews.json` (interview tracking) when you click the corresponding buttons.
- **It is also safe to expose on a LAN** if you want to check from your phone, since the only state-changing endpoints are the three interview routes and they are not authenticated. Do not expose it to the public internet without putting auth in front.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Empty home page | `~/.local/share/job-apply/` does not exist or is empty | Run Q1 at least once. |
| Market pulse all zeros | No market snapshots logged | `python job_search_apply.py --market-snapshot` |
| Q2/Q3 sections missing | `deep_apply_queue.json` is empty | Expected if you have not had any Q1 failures above 0.7 score yet. |
| `Address already in use` | Something is on 5050 | `python dashboard.py --port 8080` |
| Cover letter shows "(unable to read file)" | Cover letter was generated as `.docx` but path stored points to `.txt`, or the file was deleted | Regenerate the application or ignore. |
