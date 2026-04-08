#!/usr/bin/env python3
"""
Deep-apply computer use agent (DEPRECATED).

Superseded by assisted_apply_mcp.py which uses Playwright's structured DOM
interaction instead of screenshot+xdotool. This module is kept for fallback
use but new development should target assisted_apply_mcp.py.

Uses Claude's computer use API to control a browser on an Xvfb display
and complete job applications that failed during automated batch runs.

Usage:
    python deep_apply_computer_use.py                    # process next pending
    python deep_apply_computer_use.py --job-id li_abc    # process specific job
    python deep_apply_computer_use.py --list              # list pending queue
"""

import argparse
import base64
import json
import subprocess  # nosec B404
import sys
import time
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent))
from job_search_apply import (  # noqa: E402
    ApplicantProfile,
    _generate_deep_apply_prompt,
    _load_deep_apply_queue,
    _mark_deep_apply_done,
)

DISPLAY = ":99"
WIDTH = 1920
HEIGHT = 1080
DATA_DIR = Path.home() / ".local" / "share" / "job-apply"
MAX_TOKENS = 4096
MODEL = "claude-sonnet-4-20250514"


def _screenshot() -> str:
    """Take a screenshot of the Xvfb display, return base64-encoded PNG."""
    path = "/tmp/cu_screenshot.png"  # nosec B108
    subprocess.run(  # nosec B603 B607
        ["scrot", "--overwrite", path],
        env={"DISPLAY": DISPLAY, "HOME": str(Path.home())},
        check=True,
        capture_output=True,
    )
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode()


def _execute_action(action: dict) -> str | None:
    """Execute a computer use action, return error string or None."""
    act = action.get("action")
    env = {"DISPLAY": DISPLAY, "HOME": str(Path.home())}

    try:
        if act == "screenshot":
            return None  # handled by the loop

        elif act == "mouse_move":
            x, y = action["coordinate"]
            subprocess.run(  # nosec B603 B607
                ["xdotool", "mousemove", str(x), str(y)],
                env=env,
                check=True,
                capture_output=True,
            )

        elif act == "left_click":
            x, y = action["coordinate"]
            subprocess.run(  # nosec B603 B607
                ["xdotool", "mousemove", str(x), str(y)],
                env=env,
                check=True,
                capture_output=True,
            )
            subprocess.run(  # nosec B603 B607
                ["xdotool", "click", "1"],
                env=env,
                check=True,
                capture_output=True,
            )

        elif act == "right_click":
            x, y = action["coordinate"]
            subprocess.run(  # nosec B603 B607
                ["xdotool", "mousemove", str(x), str(y)],
                env=env,
                check=True,
                capture_output=True,
            )
            subprocess.run(  # nosec B603 B607
                ["xdotool", "click", "3"],
                env=env,
                check=True,
                capture_output=True,
            )

        elif act == "double_click":
            x, y = action["coordinate"]
            subprocess.run(  # nosec B603 B607
                ["xdotool", "mousemove", str(x), str(y)],
                env=env,
                check=True,
                capture_output=True,
            )
            subprocess.run(  # nosec B603 B607
                ["xdotool", "click", "--repeat", "2", "--delay", "100", "1"],
                env=env,
                check=True,
                capture_output=True,
            )

        elif act == "triple_click":
            x, y = action["coordinate"]
            subprocess.run(  # nosec B603 B607
                ["xdotool", "mousemove", str(x), str(y)],
                env=env,
                check=True,
                capture_output=True,
            )
            subprocess.run(  # nosec B603 B607
                ["xdotool", "click", "--repeat", "3", "--delay", "100", "1"],
                env=env,
                check=True,
                capture_output=True,
            )

        elif act == "middle_click":
            x, y = action["coordinate"]
            subprocess.run(  # nosec B603 B607
                ["xdotool", "mousemove", str(x), str(y)],
                env=env,
                check=True,
                capture_output=True,
            )
            subprocess.run(  # nosec B603 B607
                ["xdotool", "click", "2"],
                env=env,
                check=True,
                capture_output=True,
            )

        elif act == "left_click_drag":
            sx, sy = action["start_coordinate"]
            ex, ey = action["coordinate"]
            subprocess.run(  # nosec B603 B607
                ["xdotool", "mousemove", str(sx), str(sy)],
                env=env,
                check=True,
                capture_output=True,
            )
            subprocess.run(  # nosec B603 B607
                ["xdotool", "mousedown", "1"],
                env=env,
                check=True,
                capture_output=True,
            )
            subprocess.run(  # nosec B603 B607
                ["xdotool", "mousemove", str(ex), str(ey)],
                env=env,
                check=True,
                capture_output=True,
            )
            subprocess.run(  # nosec B603 B607
                ["xdotool", "mouseup", "1"],
                env=env,
                check=True,
                capture_output=True,
            )

        elif act == "type":
            text = action["text"]
            subprocess.run(  # nosec B603 B607
                ["xdotool", "type", "--clearmodifiers", "--delay", "12", text],
                env=env,
                check=True,
                capture_output=True,
            )

        elif act == "key":
            key = action["key"]
            # Map common key names to xdotool format
            key_map = {
                "Return": "Return",
                "Enter": "Return",
                "Tab": "Tab",
                "Escape": "Escape",
                "Backspace": "BackSpace",
                "Delete": "Delete",
                "space": "space",
                "Up": "Up",
                "Down": "Down",
                "Left": "Left",
                "Right": "Right",
                "Home": "Home",
                "End": "End",
                "Page_Up": "Page_Up",
                "Page_Down": "Page_Down",
                "ctrl+a": "ctrl+a",
                "ctrl+c": "ctrl+c",
                "ctrl+v": "ctrl+v",
                "ctrl+l": "ctrl+l",
            }
            xkey = key_map.get(key, key)
            subprocess.run(  # nosec B603 B607
                ["xdotool", "key", "--clearmodifiers", xkey],
                env=env,
                check=True,
                capture_output=True,
            )

        elif act == "scroll":
            coord = action.get("coordinate", [WIDTH // 2, HEIGHT // 2])
            x, y = coord
            direction = action.get("direction", action.get("scroll_direction", "down"))
            amount = action.get("amount", action.get("scroll_amount", 3))
            subprocess.run(  # nosec B603 B607
                ["xdotool", "mousemove", str(x), str(y)],
                env=env,
                check=True,
                capture_output=True,
            )
            button_map = {"up": "4", "down": "5", "left": "6", "right": "7"}
            button = button_map.get(direction, "5")
            for _ in range(amount):
                subprocess.run(  # nosec B603 B607
                    ["xdotool", "click", button],
                    env=env,
                    check=True,
                    capture_output=True,
                )

        elif act == "wait":
            time.sleep(action.get("duration", 2))

        else:
            return f"Unknown action: {act}"

    except subprocess.CalledProcessError as e:
        return f"Action {act} failed: {e.stderr.decode() if e.stderr else str(e)}"

    return None


def run_deep_apply(queue_entry: dict, profile: ApplicantProfile) -> tuple[str, str | None]:  # noqa: C901
    """
    Run computer use agent to complete a job application.

    Returns (status, reason) — status is 'submitted' or 'failed'.
    """
    prompt = _generate_deep_apply_prompt(queue_entry, profile)
    title = queue_entry["title"]
    company = queue_entry["company"]

    print(f"\n{'=' * 60}")
    print(f"🖥️  Deep-apply: {title} at {company}")
    print(f"   URL: {queue_entry['url']}")
    print(f"   Score: {queue_entry['match_score']}")
    print(f"{'=' * 60}\n")

    client = anthropic.Anthropic()

    # Take initial screenshot
    initial_screenshot = _screenshot()

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": initial_screenshot,
                    },
                },
            ],
        }
    ]

    tools = [
        {
            "type": "computer_20250124",
            "name": "computer",
            "display_width_px": WIDTH,
            "display_height_px": HEIGHT,
            "display_number": 99,
        }
    ]

    step = 0
    max_steps = 80
    status = "failed"
    reason = None

    while step < max_steps:
        step += 1
        print(f"   Step {step}/{max_steps}...", end=" ", flush=True)

        try:
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                tools=tools,
                messages=messages,
                betas=["computer-use-2025-01-24"],
            )
        except anthropic.APIError as e:
            reason = f"API error: {e}"
            print(f"❌ {reason}")
            break

        # Process response content
        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        # Check for text blocks that indicate completion
        for block in assistant_content:
            if hasattr(block, "text"):
                text = block.text.lower()
                print(f"💬 {block.text[:100]}")
                if "application submitted" in text or "successfully submitted" in text:
                    status = "submitted"
                    print("   ✅ Application submitted!")
                elif "cannot complete" in text or "unable to" in text or "failed" in text:
                    reason = block.text[:200]
                    print(f"   ❌ {reason}")

        if response.stop_reason == "end_turn":
            if status != "submitted":
                reason = reason or "Agent stopped without confirming submission"
            break

        # Execute tool calls
        tool_results = []
        for block in assistant_content:
            if block.type == "tool_use":
                tool_input = block.input
                action = tool_input.get("action")
                coord = tool_input.get("coordinate", "")
                extra = f"@{coord}" if coord else ""
                print(f"🔧 {action}{extra}", end=" ", flush=True)

                # Execute the action
                err = _execute_action(tool_input)
                if err:
                    print(f"⚠️ {err}")

                # Brief pause for UI to update
                if action in ("left_click", "type", "key", "scroll"):
                    time.sleep(0.5)

                # Take screenshot after action
                screenshot_b64 = _screenshot()

                result_content = []
                if err:
                    result_content.append({"type": "text", "text": f"Error: {err}"})
                result_content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": screenshot_b64,
                        },
                    }
                )

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_content,
                    }
                )

        if tool_results:
            messages.append({"role": "user", "content": tool_results})
            print()  # newline after action chain
        else:
            break

    if step >= max_steps and status != "submitted":
        reason = f"Exceeded max steps ({max_steps})"

    print(f"\n   Result: {status}" + (f" — {reason}" if reason else ""))
    print(f"   Steps: {step}")

    return status, reason


def main():
    parser = argparse.ArgumentParser(description="Deep-apply computer use agent")
    parser.add_argument("--job-id", help="Process specific job ID")
    parser.add_argument("--list", action="store_true", help="List pending queue entries")
    parser.add_argument("--dry-run", action="store_true", help="Generate prompt only, don't run")
    args = parser.parse_args()

    queue = _load_deep_apply_queue()
    pending = [q for q in queue if q.get("status") == "pending"]

    if args.list:
        if not pending:
            print("No pending deep-apply entries.")
            return
        print(f"\n📋 Deep-apply queue: {len(pending)} pending\n")
        for q in pending:
            print(
                f"  {q['job_id']:20s}  {q['title'][:40]:40s}  {q['company']:20s}  {q['match_score']}"
            )
        return

    # Load profile
    profile_path = DATA_DIR / "profile.json"
    if not profile_path.exists():
        print("❌ No profile.json found")
        sys.exit(1)
    profile = ApplicantProfile.from_dict(json.loads(profile_path.read_text()))

    # Select entry to process
    if args.job_id:
        entry = next((q for q in pending if q["job_id"] == args.job_id), None)
        if not entry:
            print(f"❌ Job {args.job_id} not found in pending queue")
            sys.exit(1)
    elif pending:
        entry = pending[0]
    else:
        print("No pending deep-apply entries.")
        return

    if args.dry_run:
        prompt = _generate_deep_apply_prompt(entry, profile)
        print(prompt)
        return

    # Verify display is running
    try:
        subprocess.run(  # nosec B603 B607
            ["xdotool", "getdisplaygeometry"],
            env={"DISPLAY": DISPLAY},
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(f"❌ Display {DISPLAY} not available. Start Xvfb first.")
        sys.exit(1)

    # Navigate browser to the job URL first
    url = entry["url"]
    print(f"🌐 Opening {url}")
    subprocess.run(  # nosec B603 B607
        ["xdotool", "key", "ctrl+l"],
        env={"DISPLAY": DISPLAY},
        capture_output=True,
    )
    time.sleep(0.3)
    subprocess.run(  # nosec B603 B607
        ["xdotool", "type", "--clearmodifiers", "--delay", "8", url],
        env={"DISPLAY": DISPLAY},
        capture_output=True,
    )
    subprocess.run(  # nosec B603 B607
        ["xdotool", "key", "Return"],
        env={"DISPLAY": DISPLAY},
        capture_output=True,
    )
    print("   Waiting for page load...")
    time.sleep(5)

    # Run the agent
    result_status, result_reason = run_deep_apply(entry, profile)

    # Update queue
    _mark_deep_apply_done(entry["job_id"], result_status, result_reason)
    print(f"\n{'=' * 60}")
    print(f"📋 Queue updated: {entry['job_id']} → {result_status}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
