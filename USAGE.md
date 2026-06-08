# Usage guide

Day-to-day operation. If you have not finished installation yet, read [README.md](README.md) first.

---

## The three-tier queue

```
Q1 batch apply
  submitted ----------------------> applications.json (done)
  failed (score >= 0.7, eligible) -> deep_apply_queue.json [q2, pending]
  failed (score < 0.7) -----------> applications.json (dead end)

Q2 autonomous retry
  submitted ------------> applications.json updated, queue entry done
  failed (attempts < 2) -> queue entry reset to pending
  failed (attempts >= 2) -> escalated to Q3

Q3 human escalation
  shown on dashboard with pre-computed answers for manual completion
```

### When does Q1 escalate to Q2?

Any failure where `match_score >= 0.70` and the failure category is one of:

`form_stuck`, `validation_error`, `captcha`, `no_apply_button`, `login_wall`, `modal_lost`, `max_steps`, `timeout`, `unknown_error`.

Anything below 0.70 or in a non-recoverable category stays in `applications.json` as a dead end.

### When does Q2 escalate to Q3?

After two failed Q2 attempts on the same job. Within a single attempt the agent gives up after four retries on the same page state or thirty total steps.

---

## Typical daily cycle

```bash
# 1. Check posting volumes for the day
python job_search_apply.py --market-snapshot

# 2. Q1 batch
python job_search_apply.py --max-applications 50 --min-score 0.75

# 3. Hygiene: purge stale queue entries (>7 days), escalate known-bad platforms to Q3
#    (see "Queue maintenance" below)

# 4. Q2 retry
python assisted_apply_mcp.py --max 20

# 5. Open the dashboard and finish any Q3 entries manually
python dashboard.py
```

The whole cycle takes 20 to 45 minutes of wall time depending on how many applications the batch generates.

---

## Dashboard tour

Run `python dashboard.py` and open http://localhost:5050.

| Section | What it shows |
|---------|---------------|
| Market pulse | Posting volume per title over time. Backed by `search_log.json`, fed by `--market-snapshot` runs. |
| Application table | Every Q1 application with title, company, score, status, timestamp. |
| Per-application report (`/report/<job_id>`) | Field-by-field log of what got filled, the generated cover letter, AI match reasoning. Click any row to open. |
| Q2 queue | Pending and in-progress autonomous retries. |
| Q3 queue | Entries that need you. Each shows pre-computed answers (cover letter, screening answers, reasoning) so you can finish the application by hand in under a minute. |
| Decision log | Per-application structured log of every action Q2 took (step, action, target, value, reasoning, confidence). |

---

## Working an external URL directly

Sometimes you want to apply to one specific posting outside the batch flow. Use `--external-url`. The bot bypasses LinkedIn entirely.

```bash
# Dry run against a real ATS URL
python job_search_apply.py --external-url https://jobs.ashbyhq.com/openai/abc123/application --dry-run

# Live
python job_search_apply.py --external-url https://boards.greenhouse.io/company/jobs/123
```

This is also how you iterate when fixing a per-ATS handler. Run, read the debug dump under `~/.local/share/job-apply/debug/`, refine the handler, re-run.

---

## Per-ATS notes

Real-world success rates from production runs as of 2026-04-20:

| ATS | Success rate | Notes |
|-----|--------------|-------|
| LinkedIn Easy Apply | ~67% | Most reliable path. Default for the batch loop. |
| Greenhouse | ~57% | Email verification codes via Gmail IMAP are the main hurdle. Make sure `gmail_app_password` is set. Embedded `?gh_jid=` flows on company careers pages are handled by an iframe jump. |
| Workday | ~29% | Requires an account. Set `auto_create_accounts: true`. Dropdown loops and cookie banners are handled but still fragile. |
| Paylocity | High | Resume force-upload modal handled in the platform handler. |
| Workable | High | Cookie banner backdrop dismissal handled in the platform handler. |
| SmartRecruiters | Variable | Requires SOCKS5 proxy in `proxy_rules` to bypass Incapsula WAF. |
| Comeet | Works with .docx | Silently rejects `.txt` cover letters. The bot auto-converts to .docx. |
| Ashby | ~3% | Application-level spam filter blocks most attempts. Reputation-based, proxy does not help. Manual submission is often the only way. |
| Lever | ~0% | hCaptcha is the typical blocker. Captcha key required. |
| Eightfold | Low | reCAPTCHA loops drain 2captcha credits. Kill the process if it solves more than two captchas for one application. |
| PageUp | Works | OTP emails take 2 to 7 minutes. The bot uses a 480-second timeout. |
| Alignerr | Does not work | Requires OAuth social login. Skip. |
| HRMDirect | Does not work | Start button click does not advance. Skip. |

If you see a posting from one of the "Does not work" platforms in your batch, escalate the queue entry to Q3 before running the next Q2 pass so it does not block the queue.

---

## Queue maintenance

Stale entries pile up. Run this every few days.

### Purge entries older than 7 days

```bash
python3 -c "
import json, pathlib
from datetime import datetime, timedelta
p = pathlib.Path.home() / '.local/share/job-apply/deep_apply_queue.json'
data = json.loads(p.read_text())
cutoff = datetime.now() - timedelta(days=7)
fresh = [e for e in data if datetime.fromisoformat(e['queued_at']) > cutoff]
print(f'Pruned {len(data) - len(fresh)} stale entries')
p.write_text(json.dumps(fresh, indent=2))
"
```

### Reset entries stuck at `in_progress`

If a Q2 run was killed mid-job, entries can be stuck. Reset them to `pending` or escalate to Q3:

```bash
python assisted_apply_mcp.py --list
# Manually edit deep_apply_queue.json to set status: "pending" or queue: "q3"
```

### Escalate known-bad platforms to Q3 before each Q2 run

Run `--list`, identify any Alignerr / Ashby / Eightfold / HRMDirect entries, edit their queue entry to `"queue": "q3"`. This prevents the sequential Q2 processor from hanging on a known-unsolvable site and blocking everything behind it.

---

## Cover letters

- Generated as `.docx` (Calibri 11pt) via python-docx.
- Saved to `~/.local/share/job-apply/cover-letters/`.
- Legacy `.txt` files are auto-converted to `.docx` on upload, because some ATS platforms silently reject `.txt`.
- Optional template at `~/.local/share/job-apply/cover_letter_template.txt`. Supports `{PLACEHOLDER}` tokens Claude fills in. You can append a section after `---\nTEMPLATE INSTRUCTIONS FOR AI:` for tone or style guidance.
- Point `documents.cover_letter_template_path` in your profile to the template, or leave it null for fully AI-generated letters.

---

## Hiring manager DMs

If `application_settings.message_hiring_manager: true`, the bot drafts a short LinkedIn DM to the job poster after a successful Easy Apply submission. The message is generated by Claude from the job description, your profile, and the cover letter context. Caps out at 200 tokens.

Disable it by setting the flag to false. There is no per-run flag.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `Session expired` log line | LinkedIn cookies aged out | Re-run the Step 4 capture script from the README. |
| `0 jobs found` for every title | Saturation (already applied to everything matching) or LinkedIn UI change | Try `--market-snapshot` first. If it returns counts, dedup filter is doing its job. If it returns `None`, the LinkedIn selector broke. |
| `CAPTCHA detected but no captcha_api_key configured` | `application_settings.captcha_api_key` is empty | Add a 2captcha or capsolver key. |
| `Email verification required but no gmail_app_password` | Gmail IMAP not configured | Generate a Google App Password and add it to `application_settings.gmail_app_password`. |
| Q2 hangs on one entry forever | Known-bad platform (Alignerr, Eightfold, Ashby) | Kill the process, escalate that entry to Q3, re-run. |
| `form_stuck` after multiple Q2 retries | ATS uses a captcha or login pattern not yet handled | Capture the failure URL, run `--external-url <url> --dry-run`, read the debug dump under `~/.local/share/job-apply/debug/`. If you are comfortable in the code, add a per-ATS handler under `ats_handlers/` (see [CLAUDE.md](CLAUDE.md)). |
| Dashboard shows wrong company names | `applications.json` is the source of truth. The company name is captured at apply time. | Edit the JSON manually if needed. |

---

## Safety levers

- `--dry-run` runs the full pipeline without submitting.
- `max_applications_per_day` is a hard cap. Override per-run with `--max-applications`.
- `min_match_score` sets the score floor. Override per-run with `--min-score`.
- A sensitive-field detector aborts any application that asks for SSN, bank account, passport, or similar.
- A prompt-injection detector scans job descriptions and form labels before passing them to the AI.
- Already-applied URLs are deduped against `applications.json` so a re-run never double-submits.
