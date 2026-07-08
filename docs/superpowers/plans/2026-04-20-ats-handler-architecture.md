# ATS Handler Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract all platform-specific code from the monolithic `job_search_apply.py` and `assisted_apply_mcp.py` into a composable handler registry, so ATS-specific quirks live in per-platform modules instead of growing if/else chains in shared functions.

**Architecture:** A `BaseATSHandler` abstract class defines lifecycle hooks (pre-flight checks, login wall resolution, verification code handling, success detection). A `HandlerRegistry` maps URLs to handler instances using the existing `_detect_ats_platform()` function. The generic form-filling loop calls handler hooks at defined integration points, delegating platform quirks to the handler. A `DefaultHandler` provides the current generic behavior so non-specialized platforms keep working unchanged.

**Tech Stack:** Python 3.9+, Playwright (sync API), pytest, ruff

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `ats_handlers/__init__.py` | Create | Public API: `get_handler(url)` + re-exports |
| `ats_handlers/_base.py` | Create | `BaseATSHandler` ABC with all hook signatures |
| `ats_handlers/_registry.py` | Create | `HandlerRegistry` class, maps platform names to handler classes |
| `ats_handlers/default.py` | Create | `DefaultHandler` -- current generic behavior, used for unknown platforms |
| `ats_handlers/workday.py` | Create | Workday: cookie banners, React SPA buttons, autofill popup, account creation |
| `ats_handlers/greenhouse.py` | Create | Greenhouse: verification codes via IMAP, security code input |
| `ats_handlers/smartrecruiters.py` | Create | SmartRecruiters: iframe forms, DataDome anti-bot, `/oneclick-ui/` navigation |
| `ats_handlers/lever.py` | Create | Lever stub (0% success -- just logs and bails cleanly) |
| `ats_handlers/ashby.py` | Create | Ashby stub (spam filter -- detects and bails before wasting steps) |
| `job_search_apply.py` | Modify | Add handler integration points in `_navigate_external_form` and `submit_external_apply` |
| `assisted_apply_mcp.py` | Modify | Add handler integration points in `_run_page_loop` and `process_application` |
| `tests/test_ats_handlers.py` | Create | Tests for registry, base class, and each handler |
| `tests/test_external_apply.py` | Modify | Update existing tests that mock platform-specific paths |

---

### Task 1: Create BaseATSHandler and DefaultHandler

**Files:**
- Create: `ats_handlers/__init__.py`
- Create: `ats_handlers/_base.py`
- Create: `ats_handlers/default.py`
- Test: `tests/test_ats_handlers.py`

- [ ] **Step 1: Create the `ats_handlers/` package directory**

Run: `mkdir -p ~/.openclaw/skills/job-apply/ats_handlers`

- [ ] **Step 2: Write the failing test for BaseATSHandler interface**

```python
# tests/test_ats_handlers.py
"""Tests for ATS handler registry and platform-specific handlers."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ats_handlers._base import BaseATSHandler


class TestBaseATSHandler:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            BaseATSHandler()

    def test_platform_name_is_abstract(self):
        """Subclasses must define platform_name."""
        with pytest.raises(TypeError):
            class Bad(BaseATSHandler):
                pass
            Bad()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest tests/test_ats_handlers.py::TestBaseATSHandler -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ats_handlers'`

- [ ] **Step 4: Write BaseATSHandler**

```python
# ats_handlers/_base.py
"""Base class for ATS-specific handlers.

Each handler implements lifecycle hooks called by the generic form-filling loop
in job_search_apply.py (Q1) and assisted_apply_mcp.py (Q2).  Handlers are
stateless -- all mutable state is passed via `ctx` (a plain dict).
"""

from abc import ABC, abstractmethod
from typing import Optional


class BaseATSHandler(ABC):
    """Interface for platform-specific ATS behavior.

    Lifecycle hooks (Q1 -- called from _navigate_external_form / submit_external_apply):
        pre_flight(page, ctx)            -- before form loop starts
        on_step_start(page, ctx)         -- top of each form-loop iteration
        resolve_login_wall(page, ctx)    -- when login/account wall detected
        handle_verification_code(page, ctx) -- when email verification prompt detected
        on_submit_clicked(page, ctx)     -- after Submit button click, before success check
        detect_success(page, ctx)        -- custom success detection (supplements generic)

    Lifecycle hooks (Q2 -- called from _run_page_loop / process_application):
        q2_pre_flight(page, ctx)         -- before Q2 page loop starts
        q2_resolve_login_wall(page, ctx) -- Q2 login wall handling
    """

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Return the platform name (e.g. 'Workday', 'Greenhouse')."""

    # -- Q1 hooks (job_search_apply.py) --

    def pre_flight(self, page, ctx: dict) -> Optional[str]:
        """Called once before the form-filling loop.

        Use for: dismissing cookie banners, handling popups, navigating from
        job listing to application form, DataDome retries, etc.

        Return None to continue, or a status string to abort (e.g. "failed: ...").
        `ctx` contains: 'profile', 'job', 'cover_letter_path'.
        """
        return None

    def on_step_start(self, page, ctx: dict) -> Optional[str]:
        """Called at the top of each form-loop iteration.

        Use for: platform popups that appear mid-form (Workday autofill dialog),
        dismissing overlays, etc.

        Return None to continue normal step processing, or a status string.
        Set ctx['skip_step'] = True to skip the rest of the current iteration.
        """
        return None

    def resolve_login_wall(self, page, ctx: dict) -> bool:
        """Called when a login/account wall is detected.

        Return True if the wall was resolved (continue form flow).
        Return False to use the default resolution logic.
        """
        return False

    def handle_verification_code(self, page, ctx: dict) -> Optional[str]:
        """Called when an email verification code prompt is detected.

        Return None to fall through to default handling.
        Return a status string ("submitted", "continue", "failed: ...") to override.
        """
        return None

    def on_submit_clicked(self, page, ctx: dict) -> Optional[str]:
        """Called after a Submit button is clicked, before generic success check.

        Use for: post-submit platform-specific checks, handling redirects, etc.
        Return None to continue generic checks, or a status string.
        """
        return None

    def detect_success(self, page, ctx: dict) -> bool:
        """Supplementary success detection beyond the generic check.

        Return True if this page is a success/confirmation page that the
        generic detector might miss. Return False to defer to generic detection.
        """
        return False

    # -- Q2 hooks (assisted_apply_mcp.py) --

    def q2_pre_flight(self, page, ctx: dict) -> Optional[str]:
        """Called before the Q2 page loop starts.

        Same purpose as pre_flight but for Q2 context.  ctx includes 'logger'.
        Return None to continue, or a status string to abort.
        """
        return None

    def q2_resolve_login_wall(self, page, ctx: dict) -> bool:
        """Q2-specific login wall resolution.

        Return True if resolved, False to fall through to default handling.
        """
        return False
```

- [ ] **Step 5: Write DefaultHandler**

```python
# ats_handlers/default.py
"""Default handler for unknown/unspecialized ATS platforms.

Provides no-op implementations for all hooks, meaning the generic code
path runs unchanged.  This is the handler used when no platform-specific
handler is registered.
"""

from ats_handlers._base import BaseATSHandler


class DefaultHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "unknown"
```

- [ ] **Step 6: Write `__init__.py` (empty for now -- registry comes in Task 2)**

```python
# ats_handlers/__init__.py
"""ATS handler registry -- per-platform modules for ATS-specific quirks."""
```

- [ ] **Step 7: Run test to verify it passes**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest tests/test_ats_handlers.py::TestBaseATSHandler -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
cd ~/.openclaw/skills/job-apply
git add ats_handlers/__init__.py ats_handlers/_base.py ats_handlers/default.py tests/test_ats_handlers.py
git commit -m "feat: add BaseATSHandler ABC and DefaultHandler"
```

---

### Task 2: Create HandlerRegistry

**Files:**
- Create: `ats_handlers/_registry.py`
- Modify: `ats_handlers/__init__.py`
- Test: `tests/test_ats_handlers.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ats_handlers.py`:

```python
from ats_handlers import get_handler
from ats_handlers.default import DefaultHandler


class TestHandlerRegistry:
    def test_unknown_url_returns_default(self):
        handler = get_handler("https://some-random-site.com/apply")
        assert isinstance(handler, DefaultHandler)
        assert handler.platform_name == "unknown"

    def test_workday_url_returns_workday_handler(self):
        handler = get_handler("https://company.wd5.myworkdayjobs.com/en-US/External/job/Senior-SRE")
        assert handler.platform_name == "Workday"

    def test_greenhouse_url_returns_greenhouse_handler(self):
        handler = get_handler("https://boards.greenhouse.io/company/jobs/123")
        assert handler.platform_name == "Greenhouse"

    def test_smartrecruiters_url_returns_smartrecruiters_handler(self):
        handler = get_handler("https://jobs.smartrecruiters.com/Company/12345")
        assert handler.platform_name == "SmartRecruiters"

    def test_lever_url_returns_lever_handler(self):
        handler = get_handler("https://jobs.lever.co/company/abc-123")
        assert handler.platform_name == "Lever"

    def test_ashby_url_returns_ashby_handler(self):
        handler = get_handler("https://jobs.ashbyhq.com/company/abc-123")
        assert handler.platform_name == "Ashby"

    def test_handler_is_singleton_per_platform(self):
        h1 = get_handler("https://boards.greenhouse.io/a/jobs/1")
        h2 = get_handler("https://boards.greenhouse.io/b/jobs/2")
        assert h1 is h2

    def test_all_hooks_callable_on_default(self):
        """DefaultHandler inherits all no-op hooks from BaseATSHandler."""
        handler = get_handler("https://unknown.com/apply")
        page = MagicMock()
        ctx = {}
        assert handler.pre_flight(page, ctx) is None
        assert handler.on_step_start(page, ctx) is None
        assert handler.resolve_login_wall(page, ctx) is False
        assert handler.handle_verification_code(page, ctx) is None
        assert handler.on_submit_clicked(page, ctx) is None
        assert handler.detect_success(page, ctx) is False
        assert handler.q2_pre_flight(page, ctx) is None
        assert handler.q2_resolve_login_wall(page, ctx) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest tests/test_ats_handlers.py::TestHandlerRegistry -v`
Expected: FAIL with `ImportError: cannot import name 'get_handler'`

- [ ] **Step 3: Write HandlerRegistry**

```python
# ats_handlers/_registry.py
"""Handler registry -- maps ATS platform names to handler instances.

Uses _detect_ats_platform() from job_search_apply.py for URL -> platform name
resolution.  Handlers are lazy-loaded singletons (one instance per platform).
"""

from typing import Dict, Type

from ats_handlers._base import BaseATSHandler
from ats_handlers.default import DefaultHandler

# Platform name -> handler class.  Populated by register() or at import time.
_HANDLER_MAP: Dict[str, Type[BaseATSHandler]] = {}

# Singleton cache: platform name -> handler instance
_INSTANCES: Dict[str, BaseATSHandler] = {}

_DEFAULT = DefaultHandler()


def register(platform_name: str, handler_class: Type[BaseATSHandler]) -> None:
    """Register a handler class for a platform name."""
    _HANDLER_MAP[platform_name] = handler_class
    _INSTANCES.pop(platform_name, None)  # clear cached instance


def get_handler(url: str) -> BaseATSHandler:
    """Return the appropriate handler for a URL.

    Uses _detect_ats_platform() to resolve the platform name, then looks up
    the registered handler.  Returns DefaultHandler for unknown platforms.
    """
    # Lazy import to avoid circular dependency (job_search_apply imports us)
    from job_search_apply import _detect_ats_platform

    platform = _detect_ats_platform(url)
    if platform == "unknown" or platform not in _HANDLER_MAP:
        return _DEFAULT

    if platform not in _INSTANCES:
        _INSTANCES[platform] = _HANDLER_MAP[platform]()
    return _INSTANCES[platform]
```

- [ ] **Step 4: Update `__init__.py` to export `get_handler` and trigger handler registration**

```python
# ats_handlers/__init__.py
"""ATS handler registry -- per-platform modules for ATS-specific quirks."""

from ats_handlers._registry import get_handler, register  # noqa: F401

# Import handler modules so they self-register.
# Each module calls register() at import time.
import ats_handlers.workday  # noqa: F401
import ats_handlers.greenhouse  # noqa: F401
import ats_handlers.smartrecruiters  # noqa: F401
import ats_handlers.lever  # noqa: F401
import ats_handlers.ashby  # noqa: F401
```

- [ ] **Step 5: Create stub handler files so imports don't fail**

Each stub file follows this pattern (replace platform name and URL patterns):

```python
# ats_handlers/workday.py
"""Workday ATS handler."""
from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register


class WorkdayHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Workday"


register("Workday", WorkdayHandler)
```

```python
# ats_handlers/greenhouse.py
"""Greenhouse ATS handler."""
from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register


class GreenhouseHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Greenhouse"


register("Greenhouse", GreenhouseHandler)
```

```python
# ats_handlers/smartrecruiters.py
"""SmartRecruiters ATS handler."""
from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register


class SmartRecruitersHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "SmartRecruiters"


register("SmartRecruiters", SmartRecruitersHandler)
```

```python
# ats_handlers/lever.py
"""Lever ATS handler (stub -- 0% success rate, logs and bails)."""
from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register


class LeverHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Lever"


register("Lever", LeverHandler)
```

```python
# ats_handlers/ashby.py
"""Ashby ATS handler (stub -- application-level spam filter)."""
from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register


class AshbyHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Ashby"


register("Ashby", AshbyHandler)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest tests/test_ats_handlers.py -v`
Expected: ALL PASS

- [ ] **Step 7: Run full test suite to verify no regressions**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest -v`
Expected: 206+ tests pass

- [ ] **Step 8: Lint**

Run: `cd ~/.openclaw/skills/job-apply && ruff check ats_handlers/ && ruff format --check ats_handlers/`
Expected: clean

- [ ] **Step 9: Commit**

```bash
cd ~/.openclaw/skills/job-apply
git add ats_handlers/_registry.py ats_handlers/__init__.py ats_handlers/workday.py ats_handlers/greenhouse.py ats_handlers/smartrecruiters.py ats_handlers/lever.py ats_handlers/ashby.py tests/test_ats_handlers.py
git commit -m "feat: add handler registry with stub handlers for 5 ATS platforms"
```

---

### Task 3: Integrate Handler Hooks into Q1 (`submit_external_apply` / `_navigate_external_form`)

This is the critical task. We wire handler hooks into the generic form-filling loop at defined integration points, then confirm all 206+ existing tests still pass.

**Files:**
- Modify: `job_search_apply.py` (lines ~6440-6570 in `submit_external_apply`, lines ~5907-6427 in `_navigate_external_form`)
- Test: `tests/test_ats_handlers.py`

- [ ] **Step 1: Write the failing integration test**

Add to `tests/test_ats_handlers.py`:

```python
from unittest.mock import patch, MagicMock
from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register, _HANDLER_MAP, _INSTANCES


class TestHandlerIntegration:
    """Verify that handler hooks are called during form navigation."""

    def setup_method(self):
        """Register a spy handler for testing."""
        self._orig_map = dict(_HANDLER_MAP)
        self._orig_instances = dict(_INSTANCES)

    def teardown_method(self):
        _HANDLER_MAP.clear()
        _HANDLER_MAP.update(self._orig_map)
        _INSTANCES.clear()
        _INSTANCES.update(self._orig_instances)

    def test_pre_flight_abort_stops_form_navigation(self):
        """If pre_flight returns a status string, _navigate_external_form returns it."""
        from job_search_apply import _detect_ats_platform

        class AbortHandler(BaseATSHandler):
            @property
            def platform_name(self):
                return "TestPlatform"

            def pre_flight(self, page, ctx):
                return "failed: test abort from pre_flight"

        register("TestPlatform", AbortHandler)

        # Patch _detect_ats_platform to return our test platform for any URL
        page = MagicMock()
        page.url = "https://test-platform.com/apply"
        page.query_selector_all.return_value = []
        page.frames = [page.main_frame]
        profile = MagicMock()
        job = {"id": "test_1", "title": "Test", "company": "TestCo"}

        with patch("job_search_apply._detect_ats_platform", return_value="TestPlatform"):
            from job_search_apply import _navigate_external_form
            result = _navigate_external_form(
                page, profile, job, "", True, MagicMock(), dry_run=True
            )
        assert result == "failed: test abort from pre_flight"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest tests/test_ats_handlers.py::TestHandlerIntegration::test_pre_flight_abort_stops_form_navigation -v`
Expected: FAIL (no handler hook call in `_navigate_external_form` yet)

- [ ] **Step 3: Add handler lookup to `submit_external_apply`**

In `job_search_apply.py`, add the import at the top of the file (after existing imports, around line 18):

```python
from ats_handlers import get_handler
```

In `submit_external_apply` (line ~6560, after `_final_ats_url = page.url` and before the `_is_login_wall` check), add:

```python
            # Get platform-specific handler
            handler = get_handler(page.url)
            handler_ctx = {
                "profile": profile,
                "job": job,
                "cover_letter_path": cover_letter_path,
            }
```

Then pass `handler` and `handler_ctx` to `_navigate_external_form` by adding them as parameters.

Update the `_navigate_external_form` signature (line ~5907) to accept the new parameters:

```python
def _navigate_external_form(  # noqa: C901
    page,
    profile: ApplicantProfile,
    job: dict,
    cover_letter_path: str,
    owns_browser: bool,
    context,
    dry_run: bool = False,
    handler: "BaseATSHandler | None" = None,
    handler_ctx: dict | None = None,
) -> str:
```

At the top of `_navigate_external_form` (after existing variable init, before the iframe detection block at line ~5927), add:

```python
    # Handler integration: pre_flight hook
    if handler and handler_ctx:
        abort = handler.pre_flight(page, handler_ctx)
        if abort:
            return abort
```

- [ ] **Step 4: Run the integration test to verify it passes**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest tests/test_ats_handlers.py::TestHandlerIntegration::test_pre_flight_abort_stops_form_navigation -v`
Expected: PASS

- [ ] **Step 5: Add `on_step_start` hook to the form loop**

In `_navigate_external_form`, at the top of the `for step in range(_MAX_EXTERNAL_STEPS):` loop (line ~5945, after `page.wait_for_timeout(1500)`), add:

```python
        # Handler: per-step hook
        if handler and handler_ctx:
            step_result = handler.on_step_start(page, handler_ctx)
            if step_result:
                return step_result
            if handler_ctx.get("skip_step"):
                handler_ctx.pop("skip_step")
                continue
```

- [ ] **Step 6: Add `handle_verification_code` hook**

In `_navigate_external_form`, in the verification code detection block (line ~6128, the `if profile.gmail_app_password:` block that checks for `has_verification_prompt`), add the handler hook **before** the existing verification logic:

```python
        # Handler: verification code hook (before generic handling)
        if handler and handler_ctx and has_verification_prompt:
            result = handler.handle_verification_code(page, handler_ctx)
            if result:
                if result == "submitted":
                    return "submitted"
                if result == "continue":
                    continue
                return result  # "failed: ..."
```

- [ ] **Step 7: Add `on_submit_clicked` hook**

In `_navigate_external_form`, after the submit button is clicked and before the generic success check (line ~6243, after `page.wait_for_timeout(3000)` in the `if btn_role == "submit":` block), add:

```python
            # Handler: post-submit hook
            if handler and handler_ctx:
                submit_result = handler.on_submit_clicked(page, handler_ctx)
                if submit_result:
                    return submit_result
```

- [ ] **Step 8: Add `detect_success` hook**

In `_navigate_external_form`, in the success detection block (line ~5999, the `if _detect_success_or_confirmation(page, snapshot):` check), extend it:

```python
        handler_success = handler.detect_success(page, handler_ctx) if handler and handler_ctx else False
        if handler_success or _detect_success_or_confirmation(page, snapshot):
```

- [ ] **Step 9: Run full test suite**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest -v`
Expected: 206+ tests pass (handler defaults to DefaultHandler with no-op hooks, so existing behavior is unchanged)

- [ ] **Step 10: Lint**

Run: `cd ~/.openclaw/skills/job-apply && ruff check job_search_apply.py ats_handlers/ && ruff format --check job_search_apply.py ats_handlers/`
Expected: clean

- [ ] **Step 11: Commit**

```bash
cd ~/.openclaw/skills/job-apply
git add job_search_apply.py tests/test_ats_handlers.py
git commit -m "feat: wire ATS handler hooks into Q1 form-filling loop"
```

---

### Task 4: Integrate Handler Hooks into Q2 (`_run_page_loop` / `process_application`)

**Files:**
- Modify: `assisted_apply_mcp.py` (lines ~1627-1729 in `_run_page_loop`, lines ~1760+ in `process_application`)
- Test: `tests/test_ats_handlers.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ats_handlers.py`:

```python
class TestQ2HandlerIntegration:
    def test_q2_pre_flight_abort_stops_page_loop(self):
        """If q2_pre_flight returns a status, process_application returns it."""
        from ats_handlers._base import BaseATSHandler
        from ats_handlers._registry import register

        class Q2AbortHandler(BaseATSHandler):
            @property
            def platform_name(self):
                return "TestQ2Platform"

            def q2_pre_flight(self, page, ctx):
                return "failed: Q2 test abort"

        register("TestQ2Platform", Q2AbortHandler)

        with patch("assisted_apply_mcp._detect_ats_platform", return_value="TestQ2Platform"):
            # Minimal queue entry and profile to trigger the handler
            from assisted_apply_mcp import process_application, ApplicantProfile
            queue_entry = {
                "job_id": "test_q2",
                "title": "Test",
                "company": "TestCo",
                "url": "https://testq2.com/apply",
                "match_score": 0.9,
            }
            profile = MagicMock(spec=ApplicantProfile)
            profile.resume_path = "/tmp/fake_resume.pdf"
            profile.email = "test@test.com"
            profile.gmail_app_password = None
            profile.captcha_api_key = None
            profile.proxy_rules = {}
            profile.auto_create_accounts = False

            with patch("assisted_apply_mcp._stealth_playwright"), \
                 patch("assisted_apply_mcp._playwright_context") as mock_ctx:
                mock_page = MagicMock()
                mock_page.url = "https://testq2.com/apply"
                mock_browser = MagicMock()
                mock_context = MagicMock()
                mock_ctx.return_value = (mock_browser, mock_context, mock_page, True)
                result = process_application(queue_entry, profile)

        assert result == "failed: Q2 test abort"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest tests/test_ats_handlers.py::TestQ2HandlerIntegration -v`
Expected: FAIL

- [ ] **Step 3: Add handler integration to `process_application`**

In `assisted_apply_mcp.py`, add the import near the top (after the existing `from job_search_apply import ...` block, around line 46):

```python
from ats_handlers import get_handler
```

In `process_application` (line ~1760), after the browser page is set up and before `_run_page_loop` is called, add:

```python
            handler = get_handler(page.url)
            handler_ctx = {
                "profile": profile,
                "logger": logger,
                "job_id": job_id,
                "title": title,
                "company": company,
            }

            # Handler: Q2 pre-flight
            q2_abort = handler.q2_pre_flight(page, handler_ctx)
            if q2_abort:
                logger.log("abort", "pre_flight", reasoning=q2_abort, confidence="high")
                return q2_abort
```

Pass `handler` and `handler_ctx` to `_run_page_loop`.

- [ ] **Step 4: Update `_run_page_loop` signature and add Q2 hooks**

Update the function signature (line ~1627):

```python
def _run_page_loop(page, profile, title, company, resume_path, cover_letter_path, job_id, logger, handler=None, handler_ctx=None):  # noqa: C901
```

In `_run_page_loop`, in the login wall handling block (line ~1672, the `if _handle_login_wall(page, profile, logger):` check), add the handler hook before it:

```python
        # Handler: Q2 login wall resolution
        if handler and handler_ctx:
            if handler.q2_resolve_login_wall(page, handler_ctx):
                continue
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest tests/test_ats_handlers.py::TestQ2HandlerIntegration -v`
Expected: PASS

- [ ] **Step 6: Run full test suite**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest -v`
Expected: 206+ tests pass

- [ ] **Step 7: Lint**

Run: `cd ~/.openclaw/skills/job-apply && ruff check assisted_apply_mcp.py && ruff format --check assisted_apply_mcp.py`
Expected: clean

- [ ] **Step 8: Commit**

```bash
cd ~/.openclaw/skills/job-apply
git add assisted_apply_mcp.py tests/test_ats_handlers.py
git commit -m "feat: wire ATS handler hooks into Q2 page loop"
```

---

### Task 5: Extract Workday-Specific Code into WorkdayHandler

This extracts all Workday-specific code from `_navigate_external_form` into `ats_handlers/workday.py`. The generic code paths that currently check for `myworkdayjobs.com` or `data-automation-id` get moved into handler hooks.

**Files:**
- Modify: `ats_handlers/workday.py`
- Modify: `job_search_apply.py` (remove Workday-specific blocks from `_navigate_external_form`)
- Test: `tests/test_ats_handlers.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ats_handlers.py`:

```python
from ats_handlers.workday import WorkdayHandler


class TestWorkdayHandler:
    def test_pre_flight_dismisses_cookie_banner(self):
        handler = WorkdayHandler()
        page = MagicMock()
        cookie_btn = MagicMock()
        cookie_btn.is_visible.return_value = True
        page.query_selector.return_value = cookie_btn
        ctx = {"profile": MagicMock(), "job": {}, "cover_letter_path": ""}
        result = handler.pre_flight(page, ctx)
        assert result is None  # continues
        cookie_btn.click.assert_called()  # or _safe_click

    def test_on_step_start_handles_autofill_popup(self):
        handler = WorkdayHandler()
        page = MagicMock()
        autofill_btn = MagicMock()
        autofill_btn.is_visible.return_value = True
        page.query_selector.side_effect = lambda sel: (
            autofill_btn if "autofillWithResume" in sel else None
        )
        page.url = "https://company.wd5.myworkdayjobs.com/apply"
        ctx = {"profile": MagicMock(), "job": {}, "cover_letter_path": ""}
        result = handler.on_step_start(page, ctx)
        assert result is None
        assert ctx.get("skip_step") is True

    def test_on_step_start_no_popup_returns_none(self):
        handler = WorkdayHandler()
        page = MagicMock()
        page.query_selector.return_value = None
        page.url = "https://company.wd5.myworkdayjobs.com/apply"
        ctx = {"profile": MagicMock(), "job": {}, "cover_letter_path": ""}
        result = handler.on_step_start(page, ctx)
        assert result is None
        assert "skip_step" not in ctx

    def test_login_page_not_blocked(self):
        """Workday shows 'Sign In' but allows apply without account."""
        handler = WorkdayHandler()
        page = MagicMock()
        page.url = "https://company.wd5.myworkdayjobs.com/login"
        ctx = {"profile": MagicMock()}
        # Workday handler should return True (resolved) for login pages
        # because Workday login pages have the apply form alongside
        result = handler.resolve_login_wall(page, ctx)
        assert result is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest tests/test_ats_handlers.py::TestWorkdayHandler -v`
Expected: FAIL (WorkdayHandler has no hook implementations yet)

- [ ] **Step 3: Implement WorkdayHandler hooks**

Replace `ats_handlers/workday.py` with:

```python
# ats_handlers/workday.py
"""Workday ATS handler.

Workday quirks handled here:
- Cookie consent banners (OneTrust) that overlay the form and block clicks
- "Start Your Application" popup with "Autofill with Resume" / "Apply Manually"
- React SPA button selectors (getByRole vs query_selector)
- Login pages that show "Sign In" alongside the actual apply flow
- data-automation-id selectors for Apply buttons on job listing pages
- Account creation with React checkboxes (not native <input>)
"""

import logging
from typing import Optional

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register

log = logging.getLogger("job_apply")

# Workday uses OneTrust and custom cookie banners
_COOKIE_SELECTORS = (
    "#onetrust-accept-btn-handler, "
    "[data-testid='cookie-accept'], "
    "button:has-text('Accept All'):visible, "
    "button:has-text('Accept all'):visible, "
    "button:has-text('Accept Cookies'):visible"
)

# Workday "Start Your Application" popup buttons
_AUTOFILL_SEL = "a[data-automation-id='autofillWithResume']"
_MANUAL_SEL = "a[data-automation-id='applyManually']"

# Workday Apply button on job listing pages
_APPLY_BUTTON_SEL = (
    "a[data-automation-id='jobPostingApplyButton'], "
    "button[data-automation-id='jobPostingApplyButton'], "
    "a[data-automation-id='adventureButton'], "
    "button[data-automation-id='adventureButton'], "
    "a.css-1ixbfil, "
    "a[data-uxi-element-id='Apply']"
)


class WorkdayHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Workday"

    def pre_flight(self, page, ctx: dict) -> Optional[str]:
        """Dismiss cookie banners that block Workday form interactions."""
        self._dismiss_cookie_banner(page)
        return None

    def on_step_start(self, page, ctx: dict) -> Optional[str]:
        """Handle Workday-specific mid-form popups and Apply buttons."""
        # "Start Your Application" popup -- "Autofill with Resume"
        try:
            autofill = page.query_selector(_AUTOFILL_SEL)
            if autofill and autofill.is_visible():
                log.info("   Workday popup: clicking 'Autofill with Resume'")
                from job_search_apply import _safe_click
                _safe_click(autofill, page)
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                ctx["skip_step"] = True
                return None
        except Exception:
            pass

        # Fallback: "Apply Manually"
        try:
            manual = page.query_selector(_MANUAL_SEL)
            if manual and manual.is_visible():
                log.info("   Workday popup: clicking 'Apply Manually'")
                from job_search_apply import _safe_click
                _safe_click(manual, page)
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                ctx["skip_step"] = True
                return None
        except Exception:
            pass

        # Re-dismiss cookie banner (can reappear after navigation)
        self._dismiss_cookie_banner(page)

        return None

    def resolve_login_wall(self, page, ctx: dict) -> bool:
        """Workday shows 'Sign In' alongside the apply flow.

        The classifier sees a login page, but Workday actually allows
        applying without signing in.  Return True to skip the login wall
        block and continue the form flow.
        """
        return True

    def q2_pre_flight(self, page, ctx: dict) -> Optional[str]:
        """Dismiss cookie banners before Q2 loop starts."""
        self._dismiss_cookie_banner(page)
        return None

    def q2_resolve_login_wall(self, page, ctx: dict) -> bool:
        """Same as Q1 -- Workday login pages are not blockers."""
        return True

    @staticmethod
    def _dismiss_cookie_banner(page) -> None:
        """Dismiss OneTrust or similar cookie consent overlays."""
        try:
            btn = page.query_selector(_COOKIE_SELECTORS)
            if btn and btn.is_visible():
                from job_search_apply import _safe_click
                _safe_click(btn, page)
                page.wait_for_timeout(1000)
                log.info("   Workday: dismissed cookie consent banner")
        except Exception:
            pass


register("Workday", WorkdayHandler)
```

- [ ] **Step 4: Run WorkdayHandler tests to verify they pass**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest tests/test_ats_handlers.py::TestWorkdayHandler -v`
Expected: PASS

- [ ] **Step 5: Remove Workday-specific code from `_navigate_external_form`**

In `job_search_apply.py`, remove these Workday-specific blocks from `_navigate_external_form`:

1. **Lines ~6027-6029** -- the `if "myworkdayjobs.com" not in page.url:` Workday login exception. Replace the entire login classification block:

   Before:
   ```python
           if classification["page_type"] == "login" or classification.get("has_required_login"):
               # Workday shows "Sign In" but allows apply without account
               if "myworkdayjobs.com" not in page.url:
                   return "skipped: requires account"
   ```

   After:
   ```python
           if classification["page_type"] == "login" or classification.get("has_required_login"):
               # Handler may override login wall behavior (e.g. Workday allows apply without account)
               if not (handler and handler.resolve_login_wall(page, handler_ctx or {})):
                   return "skipped: requires account"
   ```

2. **Lines ~6061-6085** -- the Workday autofill/manual popup block. Remove entirely (now handled by `WorkdayHandler.on_step_start`).

3. **Lines ~6087-6100** -- the cookie banner dismissal block in the form loop. Remove entirely (now handled by `WorkdayHandler.on_step_start` and `pre_flight`). Note: the initial `_wait_and_dismiss_cookies()` call in `submit_external_apply` (line ~6558) stays -- it's the generic pre-form cookie dismissal that all platforms use.

4. **Lines ~6102-6125** -- the job listing Apply button block. Keep this but remove the Workday-specific selectors (`data-automation-id`, `css-1ixbfil`). The Workday-specific Apply button click will be handled by `WorkdayHandler.on_step_start` in a future enhancement.

   Actually, keep this block as-is for now -- it also handles non-Workday career pages ("company career pages" comment). Moving only the Workday autofill popup is the safe extraction.

- [ ] **Step 6: Run full test suite**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest -v`
Expected: 206+ tests pass

- [ ] **Step 7: Lint**

Run: `cd ~/.openclaw/skills/job-apply && ruff check . && ruff format --check .`
Expected: clean

- [ ] **Step 8: Commit**

```bash
cd ~/.openclaw/skills/job-apply
git add ats_handlers/workday.py job_search_apply.py tests/test_ats_handlers.py
git commit -m "feat: extract Workday-specific code into WorkdayHandler"
```

---

### Task 6: Extract Greenhouse-Specific Code into GreenhouseHandler

Greenhouse's main quirk is the email verification code flow (IMAP fetch + code entry + polling for page transition).

**Files:**
- Modify: `ats_handlers/greenhouse.py`
- Modify: `job_search_apply.py` (delegate verification code handling)
- Test: `tests/test_ats_handlers.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ats_handlers.py`:

```python
from ats_handlers.greenhouse import GreenhouseHandler


class TestGreenhouseHandler:
    def test_handle_verification_code_with_code(self):
        handler = GreenhouseHandler()
        page = MagicMock()
        profile = MagicMock()
        profile.gmail_app_password = "test_password"
        profile.email = "test@test.com"
        ctx = {"profile": profile}

        # Mock: verification code found, input found, submit works
        code_input = MagicMock()
        code_input.is_visible.return_value = True
        page.get_by_label.return_value = MagicMock(count=MagicMock(return_value=1), first=code_input)

        with patch("ats_handlers.greenhouse._fetch_verification_code_from_gmail", return_value="123456"):
            with patch("ats_handlers.greenhouse._detect_success_or_confirmation", return_value=True):
                result = handler.handle_verification_code(page, ctx)
        assert result == "submitted"

    def test_handle_verification_code_no_gmail_password(self):
        handler = GreenhouseHandler()
        page = MagicMock()
        profile = MagicMock()
        profile.gmail_app_password = None
        ctx = {"profile": profile}
        result = handler.handle_verification_code(page, ctx)
        assert result is None  # falls through to generic handling
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest tests/test_ats_handlers.py::TestGreenhouseHandler -v`
Expected: FAIL

- [ ] **Step 3: Implement GreenhouseHandler**

Replace `ats_handlers/greenhouse.py` with:

```python
# ats_handlers/greenhouse.py
"""Greenhouse ATS handler.

Greenhouse quirks handled here:
- Email verification codes sent after form submission
- Security code input fields (various selector strategies)
- Post-verification page transition polling
"""

import logging
from typing import Optional

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register

log = logging.getLogger("job_apply")


class GreenhouseHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Greenhouse"

    def handle_verification_code(self, page, ctx: dict) -> Optional[str]:
        """Handle Greenhouse email verification code flow.

        Fetches code via Gmail IMAP, enters it, and polls for page transition.
        Returns 'submitted', 'continue', or 'failed: ...' status.
        Returns None if no gmail_app_password (falls through to generic handling).
        """
        profile = ctx.get("profile")
        if not profile or not profile.gmail_app_password:
            return None

        from job_search_apply import (
            _fetch_verification_code_from_gmail,
            _detect_success_or_confirmation,
            _extract_page_snapshot,
            _find_navigation_button,
            _get_field_label,
            _safe_click,
        )

        code = _fetch_verification_code_from_gmail(
            profile.email, profile.gmail_app_password, max_wait=45
        )
        if not code:
            log.warning("   Greenhouse: could not retrieve verification code from email")
            return "failed: verification code not received from email"

        # Find the security code input -- try multiple strategies
        code_input = None

        # Strategy 1: Playwright label-based locator
        for label_text in ("Security code", "Verification code", "Security Code"):
            try:
                loc = page.get_by_label(label_text)
                if loc.count() > 0 and loc.first.is_visible():
                    code_input = loc.first
                    break
            except Exception:
                continue

        # Strategy 2: attribute-based selector
        if not code_input:
            code_input = page.query_selector(
                "input[name*='security' i], input[name*='code' i], "
                "input[name*='verif' i], input[placeholder*='code' i], "
                "input[aria-label*='code' i], input[aria-label*='security' i]"
            )

        # Strategy 3: empty visible text input near "code" label
        if not code_input:
            for inp in page.query_selector_all("input[type='text'], input:not([type])"):
                try:
                    if inp.is_visible() and not inp.input_value():
                        label = _get_field_label(page, inp)
                        if label and "code" in label.lower():
                            code_input = inp
                            break
                except Exception:
                    continue

        if not code_input:
            log.warning("   Greenhouse: got code but couldn't find input field")
            return "failed: verification code input not found"

        # Fill the code using click + clear + type (React controlled components
        # reject programmatic fill)
        code_input.click()
        code_input.evaluate("el => el.value = ''")
        code_input.type(code, delay=50)
        page.wait_for_timeout(500)
        log.info("   Greenhouse: filled verification code: %s", code)

        # Submit
        submit_btn = page.query_selector(
            "button[type='submit'], input[type='submit'], button:has-text('Submit')"
        )
        if submit_btn:
            _safe_click(submit_btn, page)
        else:
            _, btn_el = _find_navigation_button(page)
            if btn_el:
                _safe_click(btn_el, page)

        page.wait_for_timeout(5000)

        # Check for success
        post_snap = _extract_page_snapshot(page)
        if _detect_success_or_confirmation(page, post_snap):
            log.info("   Greenhouse: application submitted after verification code")
            return "submitted"

        log.warning("   Greenhouse: verification code may have been rejected")
        return "failed: verification code rejected"


register("Greenhouse", GreenhouseHandler)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest tests/test_ats_handlers.py::TestGreenhouseHandler -v`
Expected: PASS

- [ ] **Step 5: Remove Greenhouse verification code block from `_navigate_external_form`**

In `job_search_apply.py`, the verification code detection block (lines ~6127-6200) currently handles Greenhouse verification inline. Now that the handler hook is wired (from Task 3 Step 6), the `GreenhouseHandler.handle_verification_code` will be called first. If the handler returns a result, the generic code is skipped.

The generic verification code block (lines ~6127-6200) should remain as a fallback for non-Greenhouse platforms that also use verification codes. No code removal needed here -- the handler hook was already wired in Task 3.

- [ ] **Step 6: Run full test suite**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest -v`
Expected: 206+ tests pass

- [ ] **Step 7: Lint**

Run: `cd ~/.openclaw/skills/job-apply && ruff check . && ruff format --check .`
Expected: clean

- [ ] **Step 8: Commit**

```bash
cd ~/.openclaw/skills/job-apply
git add ats_handlers/greenhouse.py tests/test_ats_handlers.py
git commit -m "feat: extract Greenhouse verification code flow into GreenhouseHandler"
```

---

### Task 7: Extract SmartRecruiters-Specific Code into SmartRecruitersHandler

SmartRecruiters quirks: iframe-based forms (`/oneclick-ui/`), DataDome anti-bot challenges, navigation from job listing to application form.

**Files:**
- Modify: `ats_handlers/smartrecruiters.py`
- Modify: `job_search_apply.py` (remove SmartRecruiters-specific blocks from `submit_external_apply`)
- Test: `tests/test_ats_handlers.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_ats_handlers.py`:

```python
from ats_handlers.smartrecruiters import SmartRecruitersHandler


class TestSmartRecruitersHandler:
    def test_pre_flight_navigates_to_oneclick(self):
        handler = SmartRecruitersHandler()
        page = MagicMock()
        page.url = "https://jobs.smartrecruiters.com/Company/12345"
        sr_link = MagicMock()
        sr_link.get_attribute.return_value = "https://jobs.smartrecruiters.com/Company/12345/oneclick-ui/apply"
        page.query_selector.return_value = sr_link
        ctx = {"profile": MagicMock(), "job": {}, "cover_letter_path": ""}
        result = handler.pre_flight(page, ctx)
        assert result is None  # continues
        page.goto.assert_called_once()

    def test_pre_flight_datadome_blocks(self):
        handler = SmartRecruitersHandler()
        page = MagicMock()
        page.url = "https://jobs.smartrecruiters.com/Company/12345"
        page.query_selector.return_value = None  # no oneclick link
        page.content.return_value = "<html>captcha-delivery verification required</html>"
        ctx = {"profile": MagicMock(), "job": {}, "cover_letter_path": ""}
        result = handler.pre_flight(page, ctx)
        assert result is not None
        assert "anti-bot" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest tests/test_ats_handlers.py::TestSmartRecruitersHandler -v`
Expected: FAIL

- [ ] **Step 3: Implement SmartRecruitersHandler**

Replace `ats_handlers/smartrecruiters.py` with:

```python
# ats_handlers/smartrecruiters.py
"""SmartRecruiters ATS handler.

SmartRecruiters quirks handled here:
- Job listing -> /oneclick-ui/ application form navigation
- DataDome anti-bot slider CAPTCHA (intermittent)
- Iframe-based form detection
"""

import logging
from typing import Optional

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register

log = logging.getLogger("job_apply")


class SmartRecruitersHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "SmartRecruiters"

    def pre_flight(self, page, ctx: dict) -> Optional[str]:
        """Navigate from job listing to /oneclick-ui/ form, handle DataDome."""
        url = page.url

        # Navigate from job listing to application form
        if "smartrecruiters.com" in url and "/oneclick-ui/" not in url:
            sr_link = page.query_selector(
                "a.js-o-ats-btn[href*='oneclick-ui'], a[href*='oneclick-ui']"
            )
            if sr_link:
                sr_href = sr_link.get_attribute("href")
                if sr_href:
                    log.info("   SmartRecruiters: navigating to %s", sr_href[:80])
                    page.goto(sr_href, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(3000)

        # DataDome anti-bot check
        body = page.content()[:4000].lower()
        if "captcha-delivery" in body or "verification required" in body:
            log.info("   SmartRecruiters: DataDome challenge detected, retrying...")
            page.wait_for_timeout(5000)
            page.reload(wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3000)
            body = page.content()[:4000].lower()
            if "captcha-delivery" in body or "verification required" in body:
                from job_search_apply import _dump_form_debug
                job = ctx.get("job", {})
                _dump_form_debug(page, job.get("id", ""), "DataDome anti-bot")
                return "failed: anti-bot challenge (DataDome)"

        return None


register("SmartRecruiters", SmartRecruitersHandler)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest tests/test_ats_handlers.py::TestSmartRecruitersHandler -v`
Expected: PASS

- [ ] **Step 5: Remove SmartRecruiters-specific code from `submit_external_apply`**

In `job_search_apply.py` `submit_external_apply` (lines ~6532-6555), remove:

1. **Lines ~6532-6542** -- SmartRecruiters `/oneclick-ui/` navigation block
2. **Lines ~6544-6555** -- DataDome anti-bot retry block

These are now handled by `SmartRecruitersHandler.pre_flight`.

The handler's `pre_flight` is called from `_navigate_external_form` (wired in Task 3), but this SmartRecruiters code currently runs in `submit_external_apply` (before `_navigate_external_form` is called). We need to move the handler `pre_flight` call to `submit_external_apply` as well, **or** call the handler in both places (the no-op default means double-calling is safe).

The cleanest approach: add the handler lookup + `pre_flight` call to `submit_external_apply` after `_wait_and_dismiss_cookies(page)` and before the `_is_login_wall` check (line ~6558). This is where the SmartRecruiters code currently lives. The `_navigate_external_form` `pre_flight` call stays for handlers that need it later in the flow.

Add to `submit_external_apply` (after line ~6561, `_final_ats_url = page.url`):

```python
            # Platform-specific pre-flight (SmartRecruiters navigation, DataDome, etc.)
            handler = get_handler(page.url)
            handler_ctx = {
                "profile": profile,
                "job": job,
                "cover_letter_path": cover_letter_path,
            }
            pre_flight_result = handler.pre_flight(page, handler_ctx)
            if pre_flight_result:
                return pre_flight_result

            # Update ATS URL after pre-flight navigation
            _final_ats_url = page.url
```

Then pass `handler` and `handler_ctx` to `_navigate_external_form`:

```python
            return _navigate_external_form(
                page, profile, job, cover_letter_path, owns_browser, context,
                dry_run=dry_run, handler=handler, handler_ctx=handler_ctx,
            )
```

- [ ] **Step 6: Run full test suite**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest -v`
Expected: 206+ tests pass

- [ ] **Step 7: Lint**

Run: `cd ~/.openclaw/skills/job-apply && ruff check . && ruff format --check .`
Expected: clean

- [ ] **Step 8: Commit**

```bash
cd ~/.openclaw/skills/job-apply
git add ats_handlers/smartrecruiters.py job_search_apply.py tests/test_ats_handlers.py
git commit -m "feat: extract SmartRecruiters navigation and DataDome into handler"
```

---

### Task 8: Implement Lever and Ashby Stubs

Both platforms have fundamental blockers (Lever: 0% success, Ashby: spam filter). The handlers detect these conditions early and bail with a clean status instead of wasting form-filling steps.

**Files:**
- Modify: `ats_handlers/lever.py`
- Modify: `ats_handlers/ashby.py`
- Test: `tests/test_ats_handlers.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_ats_handlers.py`:

```python
from ats_handlers.lever import LeverHandler
from ats_handlers.ashby import AshbyHandler


class TestLeverHandler:
    def test_platform_name(self):
        assert LeverHandler().platform_name == "Lever"

    def test_pre_flight_returns_none(self):
        """Lever handler doesn't abort pre-flight -- lets the form attempt proceed."""
        handler = LeverHandler()
        page = MagicMock()
        ctx = {"profile": MagicMock(), "job": {}, "cover_letter_path": ""}
        assert handler.pre_flight(page, ctx) is None


class TestAshbyHandler:
    def test_platform_name(self):
        assert AshbyHandler().platform_name == "Ashby"

    def test_detect_success_catches_spam_filter(self):
        """Ashby spam filter should NOT be detected as success."""
        handler = AshbyHandler()
        page = MagicMock()
        page.evaluate.return_value = "flagged as possible spam"
        ctx = {}
        assert handler.detect_success(page, ctx) is False

    def test_on_submit_clicked_detects_spam(self):
        handler = AshbyHandler()
        page = MagicMock()
        page.evaluate.return_value = "your application has been flagged as possible spam"
        ctx = {"job": {"id": "test"}}
        result = handler.on_submit_clicked(page, ctx)
        assert result is not None
        assert "spam" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest tests/test_ats_handlers.py::TestLeverHandler tests/test_ats_handlers.py::TestAshbyHandler -v`
Expected: FAIL (stubs have no implementations)

- [ ] **Step 3: Implement LeverHandler**

Replace `ats_handlers/lever.py`:

```python
# ats_handlers/lever.py
"""Lever ATS handler.

Lever has a 0% success rate in automated applications.  This handler
lets attempts proceed normally but doesn't add any platform-specific
logic.  If patterns emerge for why Lever fails, specific hooks can be
added here without touching generic code.
"""

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register


class LeverHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Lever"


register("Lever", LeverHandler)
```

- [ ] **Step 4: Implement AshbyHandler**

Replace `ats_handlers/ashby.py`:

```python
# ats_handlers/ashby.py
"""Ashby ATS handler.

Ashby uses an application-level spam filter that blocks automated submissions.
This is NOT IP-based (proxy doesn't help).  The handler detects the spam
rejection after form submission and returns a clean failure status.
"""

import logging
from typing import Optional

from ats_handlers._base import BaseATSHandler
from ats_handlers._registry import register

log = logging.getLogger("job_apply")


class AshbyHandler(BaseATSHandler):
    @property
    def platform_name(self) -> str:
        return "Ashby"

    def on_submit_clicked(self, page, ctx: dict) -> Optional[str]:
        """Detect Ashby spam filter rejection after submit."""
        try:
            body_text = page.evaluate(
                "() => (document.body ? document.body.innerText : '').toLowerCase()"
            )
            if "flagged as possible spam" in body_text or "flagged as spam" in body_text:
                log.warning("   Ashby: application flagged as spam")
                return "failed: Ashby spam filter"
        except Exception:
            pass
        return None


register("Ashby", AshbyHandler)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest tests/test_ats_handlers.py::TestLeverHandler tests/test_ats_handlers.py::TestAshbyHandler -v`
Expected: PASS

- [ ] **Step 6: Run full test suite**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest -v`
Expected: 206+ tests pass

- [ ] **Step 7: Commit**

```bash
cd ~/.openclaw/skills/job-apply
git add ats_handlers/lever.py ats_handlers/ashby.py tests/test_ats_handlers.py
git commit -m "feat: add Lever (passthrough) and Ashby (spam detection) handlers"
```

---

### Task 9: Final Cleanup and Full Validation

Verify the extracted code was removed, all tests pass, lint is clean, and the existing 206 tests have no regressions.

**Files:**
- Verify: all files modified in Tasks 1-8
- Modify: `CLAUDE.md` (update architecture docs)

- [ ] **Step 1: Run full test suite**

Run: `cd ~/.openclaw/skills/job-apply && python -m pytest -v`
Expected: 206+ tests pass (original 206 + new handler tests)

- [ ] **Step 2: Run linting and formatting**

Run: `cd ~/.openclaw/skills/job-apply && ruff check . && ruff format --check .`
Expected: clean

- [ ] **Step 3: Run bandit security scan**

Run: `cd ~/.openclaw/skills/job-apply && bandit -r ats_handlers/ -c pyproject.toml`
Expected: no issues

- [ ] **Step 4: Verify no remaining Workday URL checks in generic functions**

Run: `cd ~/.openclaw/skills/job-apply && grep -n 'myworkdayjobs.com' job_search_apply.py`

Expected: Only hits in `_ATS_PATTERNS` (lines ~3499-3503), `_workday_search` (lines ~1070-1212), and `_attempt_ats_login` (line ~3910 for the "Already have an account" SignIn detection). No hits in `_navigate_external_form` except via handler dispatch.

- [ ] **Step 5: Verify no remaining SmartRecruiters checks in `submit_external_apply`**

Run: `cd ~/.openclaw/skills/job-apply && grep -n 'smartrecruiters' job_search_apply.py`

Expected: Only hits in `_ATS_PATTERNS` (line ~3510), proxy resolution (line ~6454), and handler dispatch. The `oneclick-ui` and DataDome blocks should be gone from `submit_external_apply`.

- [ ] **Step 6: Update CLAUDE.md Architecture Notes**

Add under "Architecture Principles" in `CLAUDE.md`:

```markdown
### ATS Handler Registry (`ats_handlers/` package)
- `_base.py`: `BaseATSHandler` ABC with lifecycle hooks (pre_flight, on_step_start, resolve_login_wall, handle_verification_code, on_submit_clicked, detect_success, q2_pre_flight, q2_resolve_login_wall)
- `_registry.py`: `get_handler(url)` maps URLs to handler instances via `_detect_ats_platform()`
- `default.py`: `DefaultHandler` -- no-op hooks, used for unknown platforms
- `workday.py`: cookie banners, autofill popup, login wall override
- `greenhouse.py`: email verification code flow (IMAP fetch + code entry)
- `smartrecruiters.py`: `/oneclick-ui/` navigation, DataDome anti-bot
- `lever.py`: passthrough (0% success, no special handling yet)
- `ashby.py`: spam filter detection after submit

Adding a new ATS handler:
1. Create `ats_handlers/<platform>.py`
2. Subclass `BaseATSHandler`, implement relevant hooks
3. Call `register("<PlatformName>", YourHandler)` at module level
4. Add `import ats_handlers.<platform>` to `ats_handlers/__init__.py`
5. Add tests to `tests/test_ats_handlers.py`
```

- [ ] **Step 7: Commit**

```bash
cd ~/.openclaw/skills/job-apply
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md with ATS handler registry architecture"
```

- [ ] **Step 8: Test with a real Workday URL (manual verification)**

Run: `cd ~/.openclaw/skills/job-apply && python job_search_apply.py --external-url <a-workday-url> --dry-run`

Verify the handler's `pre_flight` and `on_step_start` hooks fire in the log output. The `--dry-run` flag prevents actual submission.

---

## Verification Summary

| Check | Command | Expected |
|-------|---------|----------|
| Unit tests | `pytest -v` | 206+ pass, 0 fail |
| Handler tests | `pytest tests/test_ats_handlers.py -v` | All pass |
| Lint | `ruff check . && ruff format --check .` | Clean |
| Security | `bandit -r ats_handlers/ -c pyproject.toml` | No issues |
| No Workday in generic | `grep 'myworkdayjobs.com' job_search_apply.py` | Only in `_ATS_PATTERNS` and Workday API functions |
| No SmartRecruiters in submit | `grep 'smartrecruiters' job_search_apply.py` | Only in `_ATS_PATTERNS` and proxy config |
| Dry-run Workday | `python job_search_apply.py --external-url <url> --dry-run` | Handler hooks fire |
