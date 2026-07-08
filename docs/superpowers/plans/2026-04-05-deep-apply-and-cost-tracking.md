# Deep-Apply Queue & Cost Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-application cost tracking and a deep-apply queue that re-queues high-match failed applications for a second attempt via generated Claude Chrome extension prompts.

**Architecture:** Two features sharing the application logging pipeline. Cost tracking computes `cost_usd` from existing token counts at log time and displays it on the dashboard. The deep-apply queue checks eligibility after each failed application, stores queued jobs in a separate JSON file, and exposes CLI commands (`--deep-apply`) for listing, prompt generation, and status updates. Dashboard gets new cost column, queue section, and summary stats.

**Tech Stack:** Python stdlib, Flask (existing), Jinja2 templates (existing), Chart.js (existing)

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `job_search_apply.py` | Modify | Cost calculation at log time, deep-apply queue functions, prompt generation, `--deep-apply` CLI |
| `dashboard.py` | Modify | Cost stats, queue loading, queue route, backfill cost for historical entries |
| `templates/dashboard.html` | Modify | Cost column in table, cost stat cards, deep-apply queue section |
| `templates/report.html` | Modify | Cost display on per-application detail page |
| `tests/test_cost_tracking.py` | Create | Tests for cost calculation and backfill |
| `tests/test_deep_apply.py` | Create | Tests for queue eligibility, queueing, prompt generation, status updates |

---

### Task 1: Cost Calculation at Log Time

**Files:**
- Create: `tests/test_cost_tracking.py`
- Modify: `job_search_apply.py:5425-5454` (application dict in `auto_apply_workflow`)

- [ ] **Step 1: Write the failing test for `_compute_cost_usd`**

In `tests/test_cost_tracking.py`:

```python
"""Tests for per-application cost tracking."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_search_apply import _compute_cost_usd  # noqa: E402


class TestComputeCostUsd:
    def test_zero_tokens(self):
        assert _compute_cost_usd(0, 0) == 0.0

    def test_typical_application(self):
        # 5000 input, 1000 output tokens
        # (5000 * 2.40 / 1_000_000) + (1000 * 12.00 / 1_000_000)
        # = 0.012 + 0.012 = 0.024
        result = _compute_cost_usd(5000, 1000)
        assert result == 0.024

    def test_large_token_count(self):
        # 100_000 input, 20_000 output
        # (100_000 * 2.40 / 1_000_000) + (20_000 * 12.00 / 1_000_000)
        # = 0.24 + 0.24 = 0.48
        result = _compute_cost_usd(100_000, 20_000)
        assert result == 0.48

    def test_rounds_to_four_decimals(self):
        # 1 input, 1 output
        # (1 * 2.40 / 1_000_000) + (1 * 12.00 / 1_000_000)
        # = 0.0000024 + 0.000012 = 0.0000144 -> rounds to 0.0
        result = _compute_cost_usd(1, 1)
        assert result == 0.0

    def test_output_heavy(self):
        # 0 input, 10_000 output
        # 0 + (10_000 * 12.00 / 1_000_000) = 0.12
        result = _compute_cost_usd(0, 10_000)
        assert result == 0.12
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/fedora/.openclaw/skills/job-apply && python -m pytest tests/test_cost_tracking.py -v`
Expected: FAIL with `ImportError: cannot import name '_compute_cost_usd'`

- [ ] **Step 3: Implement `_compute_cost_usd`**

In `job_search_apply.py`, add after the `_categorize_failure` function (after line 3196):

```python
# Blended token-to-dollar rates (70% Sonnet 4.6 / 30% Haiku 4.5)
_COST_INPUT_PER_M = 2.40   # $/M input tokens
_COST_OUTPUT_PER_M = 12.00  # $/M output tokens


def _compute_cost_usd(tokens_in: int, tokens_out: int) -> float:
    """Compute estimated API cost from token counts using blended rates."""
    return round(
        (tokens_in * _COST_INPUT_PER_M / 1_000_000)
        + (tokens_out * _COST_OUTPUT_PER_M / 1_000_000),
        4,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/fedora/.openclaw/skills/job-apply && python -m pytest tests/test_cost_tracking.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Add `cost_usd` to the application log entry**

In `job_search_apply.py`, in the `auto_apply_workflow` function, find the application dict (line ~5425-5454). Add `cost_usd` after the `ai_tokens` line. Change:

```python
                "ai_tokens": {"input": _ai_tokens_in, "output": _ai_tokens_out},
                "duration_seconds": round(time.time() - _apply_start_time, 1)
```

to:

```python
                "ai_tokens": {"input": _ai_tokens_in, "output": _ai_tokens_out},
                "cost_usd": _compute_cost_usd(_ai_tokens_in, _ai_tokens_out),
                "duration_seconds": round(time.time() - _apply_start_time, 1)
```

- [ ] **Step 6: Run linting**

Run: `cd /home/fedora/.openclaw/skills/job-apply && ruff check .`
Expected: No errors

- [ ] **Step 7: Commit**

```bash
cd /home/fedora/.openclaw/skills/job-apply
git add job_search_apply.py tests/test_cost_tracking.py
git commit -m "feat: add per-application cost calculation at log time

Compute cost_usd from token counts using blended rates (70% Sonnet / 30% Haiku).
New _compute_cost_usd function with 5 tests."
```

---

### Task 2: Dashboard Cost Display — Stat Cards & Table Column

**Files:**
- Modify: `dashboard.py:91-161` (stats computation and template context)
- Modify: `templates/dashboard.html:278-465` (stat cards and table)

- [ ] **Step 1: Add cost stats computation to `dashboard.py` index route**

In `dashboard.py`, in the `index()` function, add cost computation after the `hm_eligible` line (after line 144) and before `entries.sort(...)`:

```python
    # Cost tracking stats
    total_cost = 0.0
    submitted_costs = []
    failed_costs = []
    for e in entries:
        cost = e.get("cost_usd")
        if cost is None:
            # Backfill from token data for historical entries
            tokens = e.get("ai_tokens", {})
            if tokens.get("input") or tokens.get("output"):
                cost = round(
                    (tokens.get("input", 0) * 2.40 / 1_000_000)
                    + (tokens.get("output", 0) * 12.00 / 1_000_000),
                    4,
                )
        if cost:
            total_cost += cost
            if e.get("status", "").startswith("submitted"):
                submitted_costs.append(cost)
            elif e.get("status", "").startswith("failed"):
                failed_costs.append(cost)

    avg_cost_submitted = round(sum(submitted_costs) / len(submitted_costs), 4) if submitted_costs else 0.0
    avg_cost_failed = round(sum(failed_costs) / len(failed_costs), 4) if failed_costs else 0.0
```

- [ ] **Step 2: Pass cost stats to template**

In `dashboard.py`, in the `render_template` call in `index()`, add three new context variables after `hm_eligible`:

```python
        total_cost=round(total_cost, 2),
        avg_cost_submitted=avg_cost_submitted,
        avg_cost_failed=avg_cost_failed,
```

- [ ] **Step 3: Add the same cost computation to `api_data()` route**

In `dashboard.py`, in the `api_data()` function, add the same cost computation block after the `hm_eligible` line (after line 216) and before `entries.sort(...)`. Then add the three values to the returned dict:

```python
        "total_cost": round(total_cost, 2),
        "avg_cost_submitted": avg_cost_submitted,
        "avg_cost_failed": avg_cost_failed,
```

- [ ] **Step 4: Add cost stat cards to dashboard template**

In `templates/dashboard.html`, after the "Messages Sent" stat card (after line 309, before `</div>` closing `stats-row`), add:

```html
    <div class="stat-card">
        <div class="label">Total API Spend</div>
        <div class="value accent">${{ '%.2f'|format(total_cost) }}</div>
    </div>
    <div class="stat-card">
        <div class="label">Avg Cost / Submitted</div>
        <div class="value green">${{ '%.4f'|format(avg_cost_submitted) }}</div>
    </div>
    <div class="stat-card">
        <div class="label">Avg Cost / Failed</div>
        <div class="value red">${{ '%.4f'|format(avg_cost_failed) }}</div>
    </div>
```

- [ ] **Step 5: Add Cost column to application table header**

In `templates/dashboard.html`, in the table header (line ~412), add a new `<th>` after Score and before Status:

Change:
```html
                <th onclick="sortTable(4)">Score</th>
                <th onclick="sortTable(5)">Status</th>
```

to:
```html
                <th onclick="sortTable(4)">Score</th>
                <th onclick="sortTable(5)">Cost</th>
                <th onclick="sortTable(6)">Status</th>
```

Also update the Reasoning and Report column sort indices:
```html
                <th>Reasoning</th>
                <th></th>
```
(These don't have sort handlers, so no index change needed.)

- [ ] **Step 6: Add Cost column to table body rows**

In `templates/dashboard.html`, in the table body `{% for e in entries %}` loop, add the Cost cell after the Score cell (after the `</td>` closing the score bar, before the Status cell):

```html
                <td style="white-space:nowrap">
                    {% set cost = e.cost_usd if e.cost_usd is defined and e.cost_usd else none %}
                    {% if cost is none %}
                        {% set tokens = e.get('ai_tokens', {}) if e.get is defined else {} %}
                        {% set tin = tokens.get('input', 0) if tokens.get is defined else 0 %}
                        {% set tout = tokens.get('output', 0) if tokens.get is defined else 0 %}
                        {% if tin or tout %}
                            ${{ '%.4f'|format((tin * 2.4 / 1000000) + (tout * 12.0 / 1000000)) }}
                        {% else %}
                            —
                        {% endif %}
                    {% else %}
                        ${{ '%.4f'|format(cost) }}
                    {% endif %}
                </td>
```

- [ ] **Step 7: Run the dashboard locally to verify**

Run: `cd /home/fedora/.openclaw/skills/job-apply && python dashboard.py --no-browser &`
Then: `curl -s http://localhost:5050/api/data | python -m json.tool | head -30`
Then: `kill %1`

Expected: JSON response includes `total_cost`, `avg_cost_submitted`, `avg_cost_failed` keys.

- [ ] **Step 8: Run linting**

Run: `cd /home/fedora/.openclaw/skills/job-apply && ruff check .`
Expected: No errors

- [ ] **Step 9: Commit**

```bash
cd /home/fedora/.openclaw/skills/job-apply
git add dashboard.py templates/dashboard.html
git commit -m "feat: add cost tracking to dashboard

Total API spend, avg cost per submitted/failed in stat cards.
New Cost column in application table with backfill for historical entries."
```

---

### Task 3: Cost on Report Page

**Files:**
- Modify: `templates/report.html:146-186` (meta grid)

- [ ] **Step 1: Add cost and token display to the report meta grid**

In `templates/report.html`, after the "Platform" meta-item (line ~186, before `</div>` closing `meta-grid`), add:

```html
    <div class="meta-item">
        <div class="label">AI Tokens</div>
        <div class="value">
            {% if entry.ai_tokens %}
                {{ '{:,}'.format(entry.ai_tokens.input) }} in / {{ '{:,}'.format(entry.ai_tokens.output) }} out
            {% else %}
                —
            {% endif %}
        </div>
    </div>
    <div class="meta-item">
        <div class="label">API Cost</div>
        <div class="value">
            {% set cost = entry.cost_usd %}
            {% if cost is defined and cost %}
                ${{ '%.4f'|format(cost) }}
            {% elif entry.ai_tokens and (entry.ai_tokens.input or entry.ai_tokens.output) %}
                ${{ '%.4f'|format((entry.ai_tokens.input * 2.4 / 1000000) + (entry.ai_tokens.output * 12.0 / 1000000)) }}
            {% else %}
                —
            {% endif %}
        </div>
    </div>
```

- [ ] **Step 2: Run linting**

Run: `cd /home/fedora/.openclaw/skills/job-apply && ruff check .`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
cd /home/fedora/.openclaw/skills/job-apply
git add templates/report.html
git commit -m "feat: show AI tokens and cost on per-application report page"
```

---

### Task 4: Deep-Apply Queue — Eligibility & Queueing

**Files:**
- Create: `tests/test_deep_apply.py`
- Modify: `job_search_apply.py` (new constants, queue functions, integration into `auto_apply_workflow`)

- [ ] **Step 1: Write failing tests for queue eligibility**

In `tests/test_deep_apply.py`:

```python
"""Tests for deep-apply queue system."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from job_search_apply import (  # noqa: E402
    _deep_apply_eligible,
    _deep_apply_queue_path,
    _load_deep_apply_queue,
    _queue_for_deep_apply,
    _save_deep_apply_queue,
)


class TestDeepApplyEligible:
    def test_eligible_high_score_form_stuck(self):
        entry = {"match_score": 0.94, "status": "failed: form stuck", "failure_category": "form_stuck"}
        assert _deep_apply_eligible(entry, set()) is True

    def test_eligible_score_exactly_0_9(self):
        entry = {"match_score": 0.90, "status": "failed: validation", "failure_category": "validation_error"}
        assert _deep_apply_eligible(entry, set()) is True

    def test_ineligible_low_score(self):
        entry = {"match_score": 0.85, "status": "failed: form stuck", "failure_category": "form_stuck"}
        assert _deep_apply_eligible(entry, set()) is False

    def test_ineligible_timeout(self):
        entry = {"match_score": 0.95, "status": "failed: timeout", "failure_category": "timeout"}
        assert _deep_apply_eligible(entry, set()) is False

    def test_ineligible_aborted(self):
        entry = {"match_score": 0.95, "status": "aborted: injection", "failure_category": None}
        assert _deep_apply_eligible(entry, set()) is False

    def test_ineligible_other_category(self):
        entry = {"match_score": 0.95, "status": "failed: unknown", "failure_category": "other"}
        assert _deep_apply_eligible(entry, set()) is False

    def test_ineligible_already_queued(self):
        entry = {"match_score": 0.94, "status": "failed: form stuck", "failure_category": "form_stuck", "job_id": "li_abc"}
        assert _deep_apply_eligible(entry, {"li_abc"}) is False

    def test_ineligible_submitted(self):
        entry = {"match_score": 0.94, "status": "submitted", "failure_category": None}
        assert _deep_apply_eligible(entry, set()) is False

    def test_eligible_captcha(self):
        entry = {"match_score": 0.92, "status": "failed: captcha", "failure_category": "captcha"}
        assert _deep_apply_eligible(entry, set()) is True

    def test_eligible_login_wall(self):
        entry = {"match_score": 0.91, "status": "failed: login wall", "failure_category": "login_wall"}
        assert _deep_apply_eligible(entry, set()) is True

    def test_eligible_max_steps(self):
        entry = {"match_score": 0.90, "status": "failed: max steps", "failure_category": "max_steps"}
        assert _deep_apply_eligible(entry, set()) is True

    def test_eligible_no_apply_button(self):
        entry = {"match_score": 0.93, "status": "failed: no apply button", "failure_category": "no_apply_button"}
        assert _deep_apply_eligible(entry, set()) is True

    def test_eligible_modal_lost(self):
        entry = {"match_score": 0.95, "status": "failed: modal lost", "failure_category": "modal_lost"}
        assert _deep_apply_eligible(entry, set()) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/fedora/.openclaw/skills/job-apply && python -m pytest tests/test_deep_apply.py::TestDeepApplyEligible -v`
Expected: FAIL with `ImportError: cannot import name '_deep_apply_eligible'`

- [ ] **Step 3: Implement eligibility function and queue storage**

In `job_search_apply.py`, add after the `_compute_cost_usd` function:

```python
# ---------------------------------------------------------------------------
# Deep-apply queue — re-queue high-match failures for manual/vision retry
# ---------------------------------------------------------------------------

DEEP_APPLY_QUEUE_FILE = DATA_DIR / "deep_apply_queue.json"

# Failure categories eligible for deep-apply retry
_DEEP_APPLY_ELIGIBLE_CATEGORIES = frozenset({
    "form_stuck", "validation_error", "captcha",
    "no_apply_button", "login_wall", "modal_lost", "max_steps",
})


def _deep_apply_queue_path() -> Path:
    return DEEP_APPLY_QUEUE_FILE


def _load_deep_apply_queue() -> List[Dict]:
    if DEEP_APPLY_QUEUE_FILE.exists():
        try:
            return json.loads(DEEP_APPLY_QUEUE_FILE.read_text())
        except Exception:
            return []
    return []


def _save_deep_apply_queue(queue: List[Dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEEP_APPLY_QUEUE_FILE.write_text(json.dumps(queue, indent=2))


def _deep_apply_eligible(entry: Dict, already_queued_ids: set) -> bool:
    """Check if a failed application is eligible for deep-apply retry."""
    if not entry.get("status", "").startswith("failed"):
        return False
    if (entry.get("match_score") or 0) < 0.9:
        return False
    if entry.get("failure_category") not in _DEEP_APPLY_ELIGIBLE_CATEGORIES:
        return False
    if entry.get("job_id") in already_queued_ids:
        return False
    return True
```

- [ ] **Step 4: Run eligibility tests to verify they pass**

Run: `cd /home/fedora/.openclaw/skills/job-apply && python -m pytest tests/test_deep_apply.py::TestDeepApplyEligible -v`
Expected: All 13 tests PASS

- [ ] **Step 5: Write failing tests for queue storage operations**

Add to `tests/test_deep_apply.py`:

```python
class TestDeepApplyQueueStorage:
    def test_load_empty(self, tmp_path):
        with patch("job_search_apply.DEEP_APPLY_QUEUE_FILE", tmp_path / "queue.json"):
            assert _load_deep_apply_queue() == []

    def test_save_and_load(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        entry = {"job_id": "li_abc", "status": "pending"}
        with patch("job_search_apply.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("job_search_apply.DATA_DIR", tmp_path):
                _save_deep_apply_queue([entry])
            result = _load_deep_apply_queue()
        assert len(result) == 1
        assert result[0]["job_id"] == "li_abc"

    def test_load_corrupt_file(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        queue_file.write_text("not json")
        with patch("job_search_apply.DEEP_APPLY_QUEUE_FILE", queue_file):
            assert _load_deep_apply_queue() == []
```

- [ ] **Step 6: Run storage tests to verify they pass**

Run: `cd /home/fedora/.openclaw/skills/job-apply && python -m pytest tests/test_deep_apply.py::TestDeepApplyQueueStorage -v`
Expected: All 3 tests PASS

- [ ] **Step 7: Write failing test for `_queue_for_deep_apply`**

Add to `tests/test_deep_apply.py`:

```python
class TestQueueForDeepApply:
    def test_queues_eligible_entry(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        app_entry = {
            "job_id": "li_abc123",
            "title": "Senior SRE",
            "company": "Acme Corp",
            "url": "https://linkedin.com/jobs/view/123",
            "match_score": 0.94,
            "status": "failed: external form stuck (step 6/20)",
            "failure_category": "form_stuck",
            "cover_letter_path": "/tmp/cl.txt",
            "reasoning": "Strong match on SRE, Kubernetes, AWS",
        }
        with patch("job_search_apply.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("job_search_apply.DATA_DIR", tmp_path):
                with patch("job_search_apply._field_fills", [
                    {"field": "city*", "value": "Indianapolis", "source": "contact"},
                ]):
                    _queue_for_deep_apply(app_entry)

        queue = json.loads(queue_file.read_text())
        assert len(queue) == 1
        q = queue[0]
        assert q["job_id"] == "li_abc123"
        assert q["status"] == "pending"
        assert q["match_score"] == 0.94
        assert q["pre_computed"]["cover_letter_path"] == "/tmp/cl.txt"
        assert len(q["pre_computed"]["field_answers"]) == 1
        assert q["pre_computed"]["field_answers"][0]["field"] == "city*"

    def test_appends_to_existing_queue(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        queue_file.write_text(json.dumps([{"job_id": "existing", "status": "pending"}]))
        app_entry = {
            "job_id": "li_new",
            "title": "DevOps",
            "company": "Corp",
            "url": "https://example.com",
            "match_score": 0.91,
            "status": "failed: captcha",
            "failure_category": "captcha",
            "cover_letter_path": "",
            "reasoning": "",
        }
        with patch("job_search_apply.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("job_search_apply.DATA_DIR", tmp_path):
                with patch("job_search_apply._field_fills", []):
                    _queue_for_deep_apply(app_entry)

        queue = json.loads(queue_file.read_text())
        assert len(queue) == 2
        assert queue[0]["job_id"] == "existing"
        assert queue[1]["job_id"] == "li_new"
```

- [ ] **Step 8: Implement `_queue_for_deep_apply`**

In `job_search_apply.py`, add after `_deep_apply_eligible`:

```python
def _queue_for_deep_apply(app_entry: Dict) -> None:
    """Queue a failed application for deep-apply retry."""
    queue = _load_deep_apply_queue()
    queue.append({
        "job_id": app_entry["job_id"],
        "title": app_entry.get("title", ""),
        "company": app_entry.get("company", ""),
        "url": app_entry.get("url", ""),
        "match_score": app_entry.get("match_score", 0),
        "failure_reason": app_entry.get("failure_category", ""),
        "original_status": app_entry.get("status", ""),
        "queued_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pre_computed": {
            "cover_letter_path": app_entry.get("cover_letter_path", ""),
            "field_answers": list(_field_fills),
            "scoring_reasoning": app_entry.get("reasoning", ""),
        },
        "status": "pending",
        "deep_apply_status": None,
        "deep_apply_timestamp": None,
        "deep_apply_cost": None,
    })
    _save_deep_apply_queue(queue)
    log.info(
        "   \U0001f4cb Queued for deep-apply: %s - %s (score: %.2f)",
        app_entry.get("company", "?"),
        app_entry.get("title", "?"),
        app_entry.get("match_score", 0),
    )
```

- [ ] **Step 9: Run all queue tests**

Run: `cd /home/fedora/.openclaw/skills/job-apply && python -m pytest tests/test_deep_apply.py -v`
Expected: All 18 tests PASS

- [ ] **Step 10: Integrate queueing into `auto_apply_workflow`**

In `job_search_apply.py`, in `auto_apply_workflow`, after the application dict is appended to `applications` (after line ~5454, before `applied += 1`), add:

```python
        # Deep-apply queue: check if failed high-match app should be re-queued
        if status.startswith("failed"):
            _queued_ids = {q["job_id"] for q in _load_deep_apply_queue()}
            app_record = applications[-1]
            if _deep_apply_eligible(app_record, _queued_ids):
                _queue_for_deep_apply(app_record)
```

- [ ] **Step 11: Run linting**

Run: `cd /home/fedora/.openclaw/skills/job-apply && ruff check .`
Expected: No errors

- [ ] **Step 12: Commit**

```bash
cd /home/fedora/.openclaw/skills/job-apply
git add job_search_apply.py tests/test_deep_apply.py
git commit -m "feat: add deep-apply queue with eligibility checking

Queue high-match (>=0.9) failed applications for manual retry.
Eligible categories: form_stuck, validation_error, captcha, no_apply_button,
login_wall, modal_lost, max_steps. Max 1 attempt per job."
```

---

### Task 5: Deep-Apply Prompt Generation

**Files:**
- Modify: `tests/test_deep_apply.py`
- Modify: `job_search_apply.py`

- [ ] **Step 1: Write failing test for prompt generation**

Add to `tests/test_deep_apply.py`:

```python
from job_search_apply import _generate_deep_apply_prompt, ApplicantProfile  # noqa: E402


class TestGenerateDeepApplyPrompt:
    def _make_profile(self):
        return ApplicantProfile(
            full_name="Chris Draper",
            email="chris@example.com",
            phone="555-1234",
            resume_path="/home/user/resume.pdf",
            current_title="Senior SRE",
            current_employer="Acme Inc",
            years_experience=12,
            screening_answers={"salary": "150000", "city": "Indianapolis"},
        )

    def test_prompt_contains_job_details(self):
        profile = self._make_profile()
        queue_entry = {
            "job_id": "li_abc",
            "title": "Staff SRE",
            "company": "BigCo",
            "url": "https://example.com/apply",
            "match_score": 0.94,
            "pre_computed": {
                "cover_letter_path": "/tmp/cl.txt",
                "field_answers": [
                    {"field": "city*", "value": "Indianapolis", "source": "contact"},
                ],
                "scoring_reasoning": "Great match",
            },
        }
        prompt = _generate_deep_apply_prompt(queue_entry, profile)
        assert "Staff SRE" in prompt
        assert "BigCo" in prompt
        assert "https://example.com/apply" in prompt
        assert "94%" in prompt

    def test_prompt_contains_field_answers(self):
        profile = self._make_profile()
        queue_entry = {
            "job_id": "li_abc",
            "title": "SRE",
            "company": "Co",
            "url": "https://example.com",
            "match_score": 0.90,
            "pre_computed": {
                "cover_letter_path": "",
                "field_answers": [
                    {"field": "state*", "value": "Indiana", "source": "extjs_boxselect"},
                    {"field": "city*", "value": "Indianapolis", "source": "contact"},
                ],
                "scoring_reasoning": "",
            },
        }
        prompt = _generate_deep_apply_prompt(queue_entry, profile)
        assert "state*" in prompt
        assert "Indiana" in prompt
        assert "city*" in prompt
        assert "Indianapolis" in prompt

    def test_prompt_contains_screening_answers(self):
        profile = self._make_profile()
        queue_entry = {
            "job_id": "li_abc",
            "title": "SRE",
            "company": "Co",
            "url": "https://example.com",
            "match_score": 0.90,
            "pre_computed": {
                "cover_letter_path": "",
                "field_answers": [],
                "scoring_reasoning": "",
            },
        }
        prompt = _generate_deep_apply_prompt(queue_entry, profile)
        assert "salary" in prompt
        assert "150000" in prompt

    def test_prompt_contains_profile_facts(self):
        profile = self._make_profile()
        queue_entry = {
            "job_id": "li_abc",
            "title": "SRE",
            "company": "Co",
            "url": "https://example.com",
            "match_score": 0.90,
            "pre_computed": {
                "cover_letter_path": "",
                "field_answers": [],
                "scoring_reasoning": "",
            },
        }
        prompt = _generate_deep_apply_prompt(queue_entry, profile)
        assert "Acme Inc" in prompt
        assert "Senior SRE" in prompt
        assert "12" in prompt
        assert "/home/user/resume.pdf" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/fedora/.openclaw/skills/job-apply && python -m pytest tests/test_deep_apply.py::TestGenerateDeepApplyPrompt -v`
Expected: FAIL with `ImportError: cannot import name '_generate_deep_apply_prompt'`

- [ ] **Step 3: Implement `_generate_deep_apply_prompt`**

In `job_search_apply.py`, add after `_queue_for_deep_apply`:

```python
def _generate_deep_apply_prompt(queue_entry: Dict, profile: ApplicantProfile) -> str:
    """Generate a structured prompt for the Claude Chrome extension to complete an application."""
    pre = queue_entry.get("pre_computed", {})
    pct = int(queue_entry.get("match_score", 0) * 100)

    # Build field answers section
    field_lines = []
    for fa in pre.get("field_answers", []):
        field_lines.append(f"- {fa['field']}: {fa['value']}")
    field_section = "\n".join(field_lines) if field_lines else "(no pre-filled answers available)"

    # Build screening answers section
    screening_lines = []
    for k, v in profile.screening_answers.items():
        screening_lines.append(f"- {k}: {v}")
    screening_section = "\n".join(screening_lines) if screening_lines else "(none)"

    # Cover letter instruction
    cl_path = pre.get("cover_letter_path", "")
    if cl_path:
        cover_section = f"If a cover letter field appears, paste the contents of:\n{cl_path}"
    else:
        cover_section = "No cover letter was generated for this application."

    return f"""I need you to complete a job application. Follow these steps:

## Job Details
- Position: {queue_entry.get('title', 'Unknown')} at {queue_entry.get('company', 'Unknown')}
- Application URL: {queue_entry.get('url', '')}
- Match Score: {pct}%

## Step 1: Navigate
Open the application URL above.

## Step 2: Account/Login
If you see a login wall or account creation requirement:
- Create an account using: {profile.email}
- Generate a secure password
- If email verification is needed, open Gmail in a new tab, find the verification email, get the code, return and enter it

## Step 3: Fill the Application
Use these answers for form fields:

{field_section}

Additional screening answers (from profile):
{screening_section}

## Step 4: Resume
Upload my resume from: {profile.resume_path}

## Step 5: Cover Letter
{cover_section}

## Step 6: Submit
Check any consent/terms checkboxes, then click Submit.

## If you encounter anything not covered above
Use your judgment to complete the application. Key facts:
- Authorized to work in US: Yes
- Requires sponsorship: No
- Willing to relocate: No
- Open to remote: Yes
- Years of experience: {profile.years_experience or 12}
- Current employer: {profile.current_employer or 'N/A'}
- Current title: {profile.current_title or 'N/A'}"""
```

- [ ] **Step 4: Run prompt generation tests**

Run: `cd /home/fedora/.openclaw/skills/job-apply && python -m pytest tests/test_deep_apply.py::TestGenerateDeepApplyPrompt -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Run linting**

Run: `cd /home/fedora/.openclaw/skills/job-apply && ruff check .`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
cd /home/fedora/.openclaw/skills/job-apply
git add job_search_apply.py tests/test_deep_apply.py
git commit -m "feat: add deep-apply prompt generation for Chrome extension

Generates structured directive prompt with pre-computed field answers,
screening answers, resume path, and escape hatch for Claude to drive the browser."
```

---

### Task 6: Deep-Apply CLI Commands

**Files:**
- Modify: `tests/test_deep_apply.py`
- Modify: `job_search_apply.py:5798-5973` (argument parsing in `main()`)

- [ ] **Step 1: Write failing test for `_mark_deep_apply_done`**

Add to `tests/test_deep_apply.py`:

```python
from job_search_apply import _mark_deep_apply_done  # noqa: E402


class TestMarkDeepApplyDone:
    def test_marks_submitted(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        queue = [
            {"job_id": "li_abc", "status": "pending", "deep_apply_status": None,
             "deep_apply_timestamp": None, "deep_apply_cost": None},
        ]
        queue_file.write_text(json.dumps(queue))

        with patch("job_search_apply.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("job_search_apply.DATA_DIR", tmp_path):
                result = _mark_deep_apply_done("li_abc", "submitted", None)

        assert result is True
        updated = json.loads(queue_file.read_text())
        assert updated[0]["status"] == "done"
        assert updated[0]["deep_apply_status"] == "submitted"
        assert updated[0]["deep_apply_timestamp"] is not None

    def test_marks_failed_with_reason(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        queue = [
            {"job_id": "li_abc", "status": "pending", "deep_apply_status": None,
             "deep_apply_timestamp": None, "deep_apply_cost": None},
        ]
        queue_file.write_text(json.dumps(queue))

        with patch("job_search_apply.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("job_search_apply.DATA_DIR", tmp_path):
                result = _mark_deep_apply_done("li_abc", "failed", "site was down")

        assert result is True
        updated = json.loads(queue_file.read_text())
        assert updated[0]["deep_apply_status"] == "failed"

    def test_returns_false_if_not_found(self, tmp_path):
        queue_file = tmp_path / "queue.json"
        queue_file.write_text("[]")

        with patch("job_search_apply.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("job_search_apply.DATA_DIR", tmp_path):
                result = _mark_deep_apply_done("li_nonexist", "submitted", None)

        assert result is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/fedora/.openclaw/skills/job-apply && python -m pytest tests/test_deep_apply.py::TestMarkDeepApplyDone -v`
Expected: FAIL with `ImportError: cannot import name '_mark_deep_apply_done'`

- [ ] **Step 3: Implement `_mark_deep_apply_done`**

In `job_search_apply.py`, add after `_generate_deep_apply_prompt`:

```python
def _mark_deep_apply_done(job_id: str, status: str, reason: Optional[str]) -> bool:
    """Mark a deep-apply queue entry as done and update the application log."""
    queue = _load_deep_apply_queue()
    found = False
    for entry in queue:
        if entry["job_id"] == job_id:
            entry["status"] = "done"
            entry["deep_apply_status"] = status
            entry["deep_apply_timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
            found = True
            break
    if not found:
        return False
    _save_deep_apply_queue(queue)

    # Update the original application log entry
    all_apps = load_log() if LOG_FILE.exists() else []
    for app in all_apps:
        if app.get("job_id") == job_id:
            app["deep_apply_status"] = status
            app["deep_apply_timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if reason:
                app["deep_apply_reason"] = reason
            break
    if all_apps:
        LOG_FILE.write_text(json.dumps(all_apps, indent=2))

    return True
```

- [ ] **Step 4: Run mark-done tests**

Run: `cd /home/fedora/.openclaw/skills/job-apply && python -m pytest tests/test_deep_apply.py::TestMarkDeepApplyDone -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Add `--deep-apply` CLI argument to `main()`**

In `job_search_apply.py`, in `main()`, add the argument after the `--source` argument (after line ~5852):

```python
    parser.add_argument(
        "--deep-apply",
        nargs="*",
        default=None,
        metavar="CMD",
        help="Deep-apply queue: list | prompt <job_id> | prompt-all | done <job_id> --status <s>",
    )
```

- [ ] **Step 6: Add `--deep-apply` handling in `main()`**

In `job_search_apply.py`, in `main()`, add handling after `args = parser.parse_args()` and before the `if args.setup:` block (after line ~5853):

```python
    if args.deep_apply is not None:
        cmds = args.deep_apply
        cmd = cmds[0] if cmds else "list"

        if cmd == "list":
            queue = _load_deep_apply_queue()
            pending = [q for q in queue if q["status"] == "pending"]
            if not pending:
                print("No pending deep-apply entries.")
                return
            print(f"\n{'ID':<20} {'Company':<25} {'Title':<30} {'Score':>5}  {'Failure'}")
            print("-" * 110)
            for q in pending:
                print(
                    f"{q['job_id']:<20} {q['company'][:24]:<25} "
                    f"{q['title'][:29]:<30} {q['match_score']:>5.0%}  {q['failure_reason']}"
                )
            print(f"\n{len(pending)} pending entries.")
            return

        if cmd == "prompt" and len(cmds) >= 2:
            job_id = cmds[1]
            queue = _load_deep_apply_queue()
            entry = next((q for q in queue if q["job_id"] == job_id), None)
            if not entry:
                print(f"Job ID '{job_id}' not found in deep-apply queue.")
                return
            _raw = json.loads(Path(args.profile).expanduser().read_text())
            profile = ApplicantProfile.from_dict(_raw)
            print(_generate_deep_apply_prompt(entry, profile))
            return

        if cmd == "prompt-all":
            queue = _load_deep_apply_queue()
            pending = [q for q in queue if q["status"] == "pending"]
            if not pending:
                print("No pending deep-apply entries.")
                return
            _raw = json.loads(Path(args.profile).expanduser().read_text())
            profile = ApplicantProfile.from_dict(_raw)
            for q in pending:
                print(f"\n{'=' * 80}")
                print(f"# {q['company']} — {q['title']} (ID: {q['job_id']})")
                print(f"{'=' * 80}\n")
                print(_generate_deep_apply_prompt(q, profile))
            return

        if cmd == "done" and len(cmds) >= 2:
            job_id = cmds[1]
            # Parse --status and --reason from remaining args
            done_status = "submitted"
            done_reason = None
            i = 2
            while i < len(cmds):
                if cmds[i] == "--status" and i + 1 < len(cmds):
                    done_status = cmds[i + 1]
                    i += 2
                elif cmds[i] == "--reason" and i + 1 < len(cmds):
                    done_reason = cmds[i + 1]
                    i += 2
                else:
                    i += 1
            ok = _mark_deep_apply_done(job_id, done_status, done_reason)
            if ok:
                print(f"Marked {job_id} as deep-apply {done_status}.")
            else:
                print(f"Job ID '{job_id}' not found in deep-apply queue.")
            return

        parser.error(
            f"Unknown deep-apply command: {cmd}. Use: list, prompt <id>, prompt-all, done <id>"
        )

```

- [ ] **Step 7: Run full test suite**

Run: `cd /home/fedora/.openclaw/skills/job-apply && python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 8: Smoke test the CLI**

Run: `cd /home/fedora/.openclaw/skills/job-apply && python job_search_apply.py --deep-apply list`
Expected: Prints "No pending deep-apply entries." (or lists any entries if the queue file exists)

- [ ] **Step 9: Run linting**

Run: `cd /home/fedora/.openclaw/skills/job-apply && ruff check .`
Expected: No errors

- [ ] **Step 10: Commit**

```bash
cd /home/fedora/.openclaw/skills/job-apply
git add job_search_apply.py tests/test_deep_apply.py
git commit -m "feat: add --deep-apply CLI for queue management and prompt generation

Commands: list, prompt <id>, prompt-all, done <id> --status <s> --reason <r>.
Marks queue entries done and updates application log with deep-apply status."
```

---

### Task 7: Dashboard — Deep-Apply Queue Section & Method Indicator

**Files:**
- Modify: `dashboard.py`
- Modify: `templates/dashboard.html`

- [ ] **Step 1: Load deep-apply queue in dashboard routes**

In `dashboard.py`, add the import at the top (after `from pathlib import Path`):

```python
DEEP_APPLY_QUEUE_FILE = DATA_DIR / "deep_apply_queue.json"
```

In the `index()` function, add after the cost stats block and before `entries.sort(...)`:

```python
    # Deep-apply queue
    deep_queue = _load_json(DEEP_APPLY_QUEUE_FILE)
    deep_pending = [q for q in deep_queue if q.get("status") == "pending"]
    deep_done = [q for q in deep_queue if q.get("status") == "done"]
    deep_success = sum(1 for q in deep_done if q.get("deep_apply_status") == "submitted")
```

Add to the `render_template` call:

```python
        deep_queue=deep_queue,
        deep_pending_count=len(deep_pending),
        deep_success_count=deep_success,
        deep_done_count=len(deep_done),
```

- [ ] **Step 2: Add deep-apply data to `api_data()` route**

In `dashboard.py`, in the `api_data()` function, add the same queue loading after cost stats and add to the returned dict:

```python
        "deep_pending_count": len(deep_pending),
        "deep_success_count": deep_success,
        "deep_done_count": len(deep_done),
```

- [ ] **Step 3: Add deep-apply stat cards to dashboard template**

In `templates/dashboard.html`, after the cost stat cards (the ones added in Task 2), add:

```html
    <div class="stat-card">
        <div class="label">Deep-Apply Pending</div>
        <div class="value amber">{{ deep_pending_count }}</div>
    </div>
    <div class="stat-card">
        <div class="label">Deep-Apply Success</div>
        <div class="value green">{{ deep_success_count }} / {{ deep_done_count }} attempted</div>
    </div>
```

- [ ] **Step 4: Add deep-apply queue table section**

In `templates/dashboard.html`, after the score distribution chart section (after `</div>` closing `charts-full` for scoreHistChart, before the "Application table" comment), add:

```html
<!-- Deep-Apply Queue -->
{% if deep_queue %}
<div class="table-card" style="margin-bottom: 2rem;">
    <div class="table-header">
        <h2>Deep-Apply Queue</h2>
    </div>
    <table>
        <thead>
            <tr>
                <th>Title</th>
                <th>Company</th>
                <th>Score</th>
                <th>Failure</th>
                <th>Queued</th>
                <th>Status</th>
                <th>ID</th>
            </tr>
        </thead>
        <tbody>
            {% for q in deep_queue %}
            <tr>
                <td class="truncate"><a href="{{ q.url }}" target="_blank">{{ q.title }}</a></td>
                <td>{{ q.company }}</td>
                <td>{{ '%.0f'|format((q.match_score or 0) * 100) }}%</td>
                <td>{{ q.failure_reason }}</td>
                <td style="white-space:nowrap">{{ q.queued_at[:10] if q.queued_at else '—' }}</td>
                <td>
                    {% if q.status == 'pending' %}
                        <span class="badge badge-dryrun">Pending</span>
                    {% elif q.deep_apply_status == 'submitted' %}
                        <span class="badge badge-submitted">Submitted</span>
                    {% elif q.deep_apply_status == 'failed' %}
                        <span class="badge badge-failed">Failed</span>
                    {% else %}
                        <span class="badge badge-other">{{ q.status }}</span>
                    {% endif %}
                </td>
                <td style="font-size:0.75rem;color:var(--text-muted)">{{ q.job_id }}</td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
</div>
{% endif %}
```

- [ ] **Step 5: Add method indicator to the application table**

In `templates/dashboard.html`, in the application table body, update the Status cell to show deep-apply status when present. Change the status `<td>` to:

```html
                <td>
                    {% set st = e.status|default('unknown') %}
                    {% if st.startswith('submitted') %}
                        <span class="badge badge-submitted">Submitted</span>
                    {% elif st.startswith('aborted') %}
                        <span class="badge badge-aborted">Aborted</span>
                    {% elif st.startswith('failed') %}
                        <span class="badge badge-failed">Failed</span>
                    {% elif st == 'dry_run' %}
                        <span class="badge badge-dryrun">Dry Run</span>
                    {% else %}
                        <span class="badge badge-other">{{ st }}</span>
                    {% endif %}
                    {% if e.deep_apply_status %}
                        <br><span style="font-size:0.7rem;color:var(--text-muted)">
                            deep-apply: <span style="color:{% if e.deep_apply_status == 'submitted' %}var(--green){% else %}var(--red){% endif %}">{{ e.deep_apply_status }}</span>
                        </span>
                    {% endif %}
                </td>
```

- [ ] **Step 6: Run the dashboard to verify**

Run: `cd /home/fedora/.openclaw/skills/job-apply && python dashboard.py --no-browser &`
Then: `curl -s http://localhost:5050/api/data | python -m json.tool | grep deep`
Then: `kill %1`

Expected: JSON response includes `deep_pending_count`, `deep_success_count`, `deep_done_count`.

- [ ] **Step 7: Run linting**

Run: `cd /home/fedora/.openclaw/skills/job-apply && ruff check .`
Expected: No errors

- [ ] **Step 8: Commit**

```bash
cd /home/fedora/.openclaw/skills/job-apply
git add dashboard.py templates/dashboard.html
git commit -m "feat: add deep-apply queue section and method indicator to dashboard

Queue table shows pending/completed entries. Application table shows
deep-apply status as secondary line. Stat cards for pending/success counts."
```

---

### Task 8: Full Integration Test & Final Cleanup

**Files:**
- Modify: `tests/test_deep_apply.py`
- All files (linting pass)

- [ ] **Step 1: Write integration test for the full queue-and-prompt flow**

Add to `tests/test_deep_apply.py`:

```python
class TestDeepApplyIntegration:
    def test_full_queue_prompt_done_flow(self, tmp_path):
        """End-to-end: queue an entry, generate prompt, mark done."""
        queue_file = tmp_path / "queue.json"
        log_file = tmp_path / "applications.json"

        app_entry = {
            "job_id": "li_integ",
            "title": "Platform Engineer",
            "company": "TestCo",
            "url": "https://example.com/apply/123",
            "match_score": 0.92,
            "status": "failed: external form stuck (step 4/15)",
            "failure_category": "form_stuck",
            "cover_letter_path": "/tmp/cl_integ.txt",
            "reasoning": "Strong K8s and AWS match",
        }

        # Write a mock application log
        log_file.write_text(json.dumps([app_entry]))

        profile = ApplicantProfile(
            full_name="Test User",
            email="test@example.com",
            phone="555-0000",
            resume_path="/tmp/resume.pdf",
            current_title="SRE",
            current_employer="CurrentCo",
            years_experience=10,
            screening_answers={"salary": "150000"},
        )

        with patch("job_search_apply.DEEP_APPLY_QUEUE_FILE", queue_file):
            with patch("job_search_apply.DATA_DIR", tmp_path):
                with patch("job_search_apply._field_fills", [
                    {"field": "city", "value": "Indy", "source": "contact"},
                ]):
                    # Step 1: Check eligibility
                    assert _deep_apply_eligible(app_entry, set()) is True

                    # Step 2: Queue it
                    _queue_for_deep_apply(app_entry)
                    queue = _load_deep_apply_queue()
                    assert len(queue) == 1
                    assert queue[0]["status"] == "pending"

                    # Step 3: Generate prompt
                    prompt = _generate_deep_apply_prompt(queue[0], profile)
                    assert "Platform Engineer" in prompt
                    assert "TestCo" in prompt
                    assert "city" in prompt
                    assert "/tmp/resume.pdf" in prompt

                    # Step 4: Mark done
                    with patch("job_search_apply.LOG_FILE", log_file):
                        ok = _mark_deep_apply_done("li_integ", "submitted", None)
                    assert ok is True

                    # Verify queue updated
                    updated_queue = _load_deep_apply_queue()
                    assert updated_queue[0]["status"] == "done"
                    assert updated_queue[0]["deep_apply_status"] == "submitted"

                    # Verify app log updated
                    updated_log = json.loads(log_file.read_text())
                    assert updated_log[0]["deep_apply_status"] == "submitted"
```

- [ ] **Step 2: Update the import block at the top of `tests/test_deep_apply.py`**

Make sure the import block includes all needed names:

```python
from job_search_apply import (  # noqa: E402
    _deep_apply_eligible,
    _deep_apply_queue_path,
    _generate_deep_apply_prompt,
    _load_deep_apply_queue,
    _mark_deep_apply_done,
    _queue_for_deep_apply,
    _save_deep_apply_queue,
    ApplicantProfile,
)
```

- [ ] **Step 3: Run the full test suite**

Run: `cd /home/fedora/.openclaw/skills/job-apply && python -m pytest tests/ -v`
Expected: All tests PASS (existing + new cost tracking + new deep-apply tests)

- [ ] **Step 4: Run linting on everything**

Run: `cd /home/fedora/.openclaw/skills/job-apply && ruff check . && ruff format --check .`
Expected: No errors, no formatting changes needed

- [ ] **Step 5: Commit**

```bash
cd /home/fedora/.openclaw/skills/job-apply
git add tests/test_deep_apply.py
git commit -m "test: add end-to-end integration test for deep-apply flow

Tests the full cycle: eligibility check, queueing, prompt generation,
marking done, and verifying both queue and application log are updated."
```

---

## Summary of New/Modified Files

| File | Changes |
|------|---------|
| `job_search_apply.py` | `_compute_cost_usd()`, `cost_usd` in log entry, `_DEEP_APPLY_ELIGIBLE_CATEGORIES`, `_deep_apply_eligible()`, `_load/save_deep_apply_queue()`, `_queue_for_deep_apply()`, `_generate_deep_apply_prompt()`, `_mark_deep_apply_done()`, `--deep-apply` CLI, queue integration in `auto_apply_workflow` |
| `dashboard.py` | Cost stats computation + backfill, deep-apply queue loading, new template context vars |
| `templates/dashboard.html` | 3 cost stat cards, Cost table column, 2 deep-apply stat cards, deep-apply queue table, method indicator |
| `templates/report.html` | AI Tokens + API Cost meta items |
| `tests/test_cost_tracking.py` | 5 tests for `_compute_cost_usd` |
| `tests/test_deep_apply.py` | ~22 tests: eligibility (13), storage (3), queueing (2), prompt gen (4), mark-done (3), integration (1) |
