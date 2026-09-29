"""Tests for the Workday vision act loop's handling of model turns."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ats_handlers import _workday_vision as wv  # noqa: E402


def _text_turn(text="Let me look at the page first."):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=MagicMock())


def _done_turn():
    tu = SimpleNamespace(type="tool_use", id="tu1", input={"action": "done", "reason": "finished"})
    return SimpleNamespace(content=[tu], usage=MagicMock())


def _run(turns, max_actions=5):
    client = MagicMock()
    client.messages.create.side_effect = turns
    with (
        patch.object(wv, "_screen", return_value=[]),
        patch.object(wv, "_autofill_resume"),
    ):
        result = wv._run_act_loop(MagicMock(), client, "system", "user", max_actions)
    return result, client


class TestActLoopToolChoice:
    def test_does_not_force_tool_use(self):
        # Sonnet 5.5 rejects tool_choice "any" and "tool" with HTTP 400.
        _result, client = _run([_done_turn()])
        assert client.messages.create.call_args.kwargs["tool_choice"] == {"type": "auto"}

    def test_text_only_turn_is_nudged_back_to_the_tool(self):
        result, client = _run([_text_turn(), _done_turn()])
        assert result is True
        # messages: [task, assistant text, nudge, assistant tool_use]
        messages = client.messages.create.call_args.kwargs["messages"]
        assert messages[2]["role"] == "user"
        assert "act tool" in str(messages[2]["content"])

    def test_gives_up_after_two_text_only_turns_in_a_row(self):
        result, client = _run([_text_turn(), _text_turn(), _done_turn()])
        assert result is False
        assert client.messages.create.call_count == 2
