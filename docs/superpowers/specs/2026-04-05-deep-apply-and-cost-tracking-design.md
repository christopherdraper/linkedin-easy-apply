# Deep-Apply Queue & Cost Tracking

**Date:** 2026-04-05
**Status:** Approved

## Problem

The standard automated apply flow fails on many ATS platforms due to custom widget libraries (ExtJS, MUI, Avature SPAs), anti-bot detection, and form navigation edge cases. Each ATS requires bespoke CSS selector logic that grows the codebase without proportionally improving reliability. Meanwhile, we have no visibility into per-application API spend.

## Solution

Two changes:

1. **Cost tracking** — compute and surface per-application API cost on the dashboard.
2. **Deep-apply queue** — when a high-match application fails the standard flow, queue it for a second attempt driven by Claude via the Chrome extension. Generate a structured prompt the user pastes into the extension. Start manual (phase C of build-measure-learn), graduate to Playwright automation later once the prompt is dialed in.

## Cost Tracking

### Token-to-Dollar Calculation

Pricing constants (hardcoded, updated when models change):

| Model | Input ($/M tokens) | Output ($/M tokens) |
|-------|-------------------:|--------------------:|
| Haiku 4.5 | 1.00 | 5.00 |
| Sonnet 4.6 | 3.00 | 15.00 |

Current code tracks `_ai_tokens_in` and `_ai_tokens_out` per application across all API calls (scoring, cover letter, form answers, page classification, vision button finding). These are already logged to `applications.json` as `ai_tokens: {input, output}`.

The majority of calls use Sonnet 4.6; scoring and notes use Haiku 4.5. Since we don't track per-call model breakdown, use a blended rate. Approximate 70% Sonnet / 30% Haiku by token volume:

- **Blended input rate:** $2.40/M tokens
- **Blended output rate:** $12.00/M tokens

Alternatively, track model per-call for exact cost. Recommendation: use the blended rate for v1, revisit if precision matters.

### Application Log Changes

Add `cost_usd` field to each application log entry, computed at log time:

```python
cost_usd = round(
    (ai_tokens_in * 2.40 / 1_000_000) + (ai_tokens_out * 12.00 / 1_000_000), 4
)
```

### Dashboard Changes

**Application table:**
- New "Cost" column showing `$0.03` format

**Per-application report page:**
- Cost displayed alongside existing token counts

**Summary stats (top of dashboard):**
- Total API spend across all applications
- Average cost per submitted application
- Average cost per failed application

Historical entries with `ai_tokens` data but no `cost_usd` get backfill-computed on dashboard load (calculated in-memory at render time, not written back to `applications.json`).

## Deep-Apply Queue

### Eligibility

A failed application is queued for deep-apply when ALL of these are true:

- Match score >= 0.9
- Failure category is one of: `form_stuck`, `validation_error`, `captcha`, `no_apply_button`, `login_wall`, `modal_lost`, `max_steps`
- Not previously attempted via deep-apply (max 1 attempt per job)

### Ineligible Failures

- `timeout` — site issue, not a form problem
- `aborted` — injection/suspicious field safety mechanism, do not bypass
- `other` — uncategorized failures

### Queue Storage

New file: `~/.local/share/job-apply/deep_apply_queue.json`

```json
[
  {
    "job_id": "li_abc123",
    "title": "Senior SRE",
    "company": "Acme Corp",
    "url": "https://linkedin.com/jobs/view/...",
    "match_score": 0.94,
    "failure_reason": "form_stuck",
    "original_status": "failed: external form stuck (step 6/20)",
    "queued_at": "2026-04-05 03:00:00",
    "pre_computed": {
      "cover_letter_path": "/path/to/cover_letter.txt",
      "field_answers": [
        {"field": "city*", "value": "Indianapolis", "source": "contact"},
        {"field": "state*", "value": "Indiana", "source": "extjs_boxselect"}
      ],
      "scoring_reasoning": "Strong match on SRE, Kubernetes, AWS..."
    },
    "status": "pending",
    "deep_apply_status": null,
    "deep_apply_timestamp": null,
    "deep_apply_cost": null
  }
]
```

### Queueing Behavior

During the regular batch run, after logging a failed application:

1. Check eligibility (score >= 0.9, eligible failure category, not already queued)
2. Build `pre_computed` from the current application state (`_field_fills`, cover letter path, scoring data)
3. Append to `deep_apply_queue.json`
4. Log: `"📋 Queued for deep-apply: <company> - <title> (score: 0.94)"`

## Prompt Generation

### CLI Interface

```bash
# List pending queue entries
python job_search_apply.py --deep-apply list

# Generate prompt for one job (prints to stdout)
python job_search_apply.py --deep-apply prompt <job_id>

# Generate prompts for all pending jobs
python job_search_apply.py --deep-apply prompt-all

# Mark job as completed after manual attempt
python job_search_apply.py --deep-apply done <job_id> --status submitted

# Mark job as failed after manual attempt
python job_search_apply.py --deep-apply done <job_id> --status failed --reason "site was down"
```

### Prompt Structure

Directive script with escape hatch. The prompt gives Claude step-by-step instructions using pre-computed data from the first pass, ending with a general instruction to use judgment for anything not covered.

```
I need you to complete a job application. Follow these steps:

## Job Details
- Position: {title} at {company}
- Application URL: {url}
- Match Score: {match_score}%

## Step 1: Navigate
Open the application URL above.

## Step 2: Account/Login
If you see a login wall or account creation requirement:
- Create an account using: {email}
- Generate a secure password
- If email verification is needed, open Gmail in a new tab,
  find the verification email, get the code, return and enter it

## Step 3: Fill the Application
Use these answers for form fields:

{for each field_answer:}
- {field}: {value}

Additional screening answers (from profile.json screening_answers):
{all screening_answers as key: value pairs}

## Step 4: Resume
Upload my resume from: {resume_path}

## Step 5: Cover Letter
If a cover letter field appears, paste the contents of:
{cover_letter_path}

## Step 6: Submit
Check any consent/terms checkboxes, then click Submit.

## If you encounter anything not covered above
Use your judgment to complete the application. Key facts (from profile.json):
- Authorized to work in US: Yes
- Requires sponsorship: No
- Willing to relocate: No
- Open to remote: Yes
- Years of experience: 12
- Current employer: {profile.current_employer}
- Current title: {profile.current_title}
```

### After Manual Completion

`--deep-apply done <job_id> --status <status>` does:

1. Update queue entry: set `status`, `deep_apply_status`, `deep_apply_timestamp`
2. Update the original application log entry with `deep_apply_status` and `deep_apply_timestamp`
3. `deep_apply_cost` stays null for manual Chrome extension attempts (no token data available)

## Dashboard Integration

### Application Table

- New "Cost" column — `$0.03` format
- New "Method" indicator — `standard` or `deep-apply` badge
- Deep-apply status shown as secondary line: `failed: form_stuck → deep-apply: submitted`

### Deep-Apply Queue Section

New section on dashboard showing:

| Title | Company | Score | Failure | Queued | Status |
|-------|---------|-------|---------|--------|--------|

With a "Generate Prompt" link/button per entry that displays the prompt.

### Summary Stats Additions

- Deep-apply queue: pending count, success count
- Success rate comparison: standard vs deep-apply

## Files Modified

- `job_search_apply.py` — cost calculation at log time, queue management functions, prompt generation, `--deep-apply` CLI argument parsing
- `dashboard.py` — cost column, queue section route, stats computation
- `templates/dashboard.html` — cost column, queue table, stats display
- `templates/report.html` — cost on per-application detail page

## New Files

- `~/.local/share/job-apply/deep_apply_queue.json` — runtime queue data (not in repo)

## No New Dependencies

Everything uses existing Anthropic API, Playwright, Flask, stdlib.

## Future: Phase B Automation

Once the prompt is refined through manual usage, automate with Playwright:

- `_vision_fill_form(page, profile, pre_computed)` function
- Screenshot → Claude Sonnet vision → Playwright action execution loop
- Augmented with `_extract_page_snapshot()` for DOM context (better than Chrome extension)
- 2Captcha integration exposed as a tool Claude can invoke during the vision loop
- `--deep-apply run` CLI flag to process queue automatically
- Token tracking for automated attempts → `deep_apply_cost` populated

This is out of scope for the current implementation.
