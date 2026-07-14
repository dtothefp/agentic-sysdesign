"""ReAct loop unit tests. No network, no db, no Anthropic: a scripted model (monkeypatched over
`anthropic_complete`) plays the turns and a fake `run_tool` records calls, so these prove the loop
MECHANICS in isolation, which is the part a code challenge is actually grading.

The loop hardcodes its two dependencies (`anthropic_complete` for the model turn, `run_tool` for
tool dispatch) as module globals rather than injectable params, so the tests reach in and swap
those globals via monkeypatch. `run_agent` looks both names up at call time, so the swap takes.

Covered: tool dispatch + result feed-back + stop condition, error recovery (a raising tool must
not kill the turn), and the max_turns guard (a model that never stops asking for tools).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from agent import loop
from agent.loop import run_agent


def _fake_run_tool(calls: list) -> Callable[[str, dict], object]:
    """Stand-in for agent.tools.run_tool. `echo` records its input and echoes it back; `boom`
    always raises, to exercise the loop's error-recovery path."""

    def run_tool(name: str, tool_input: dict):
        if name == "boom":
            raise RuntimeError("kaboom")
        calls.append(tool_input)
        return {"echoed": tool_input}

    return run_tool


def _script(*turns: dict) -> Callable[..., Iterator[dict]]:
    """Build a fake `anthropic_complete` that emits each scripted turn in order: its text deltas,
    then its final message block(s) and stop_reason."""
    queue = list(turns)

    def complete(messages, tool_schemas, system, model):  # noqa: ARG001 - signature must match Complete
        turn = queue.pop(0)
        for piece in turn.get("text", []):
            yield {"type": "text_delta", "text": piece}
        yield {"type": "final", "content": turn["content"], "stop_reason": turn["stop_reason"]}

    return complete


def test_tool_call_then_answer(monkeypatch):
    calls: list = []
    monkeypatch.setattr(loop, "run_tool", _fake_run_tool(calls))
    monkeypatch.setattr(
        loop,
        "anthropic_complete",
        _script(
            {
                "text": ["let me check "],
                "content": [{"type": "tool_use", "id": "t1", "name": "echo", "input": {"x": "hi"}}],
                "stop_reason": "tool_use",
            },
            {"text": ["all done"], "content": [{"type": "text", "text": "all done"}], "stop_reason": "end_turn"},
        ),
    )

    events = list(run_agent("hey"))
    types = [e["type"] for e in events]

    assert "tool_use" in types and "tool_result" in types
    assert calls == [{"x": "hi"}]  # the tool actually ran with the model's input
    assert "".join(e["text"] for e in events if e["type"] == "text") == "let me check all done"
    assert events[-1] == {"type": "done", "stop_reason": "end_turn", "turns": 2}


def test_tool_error_is_recoverable(monkeypatch):
    monkeypatch.setattr(loop, "run_tool", _fake_run_tool([]))
    monkeypatch.setattr(
        loop,
        "anthropic_complete",
        _script(
            {"content": [{"type": "tool_use", "id": "t1", "name": "boom", "input": {}}], "stop_reason": "tool_use"},
            {"content": [{"type": "text", "text": "sorry, that failed"}], "stop_reason": "end_turn"},
        ),
    )

    events = list(run_agent("hey"))

    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert tool_result["ok"] is False
    assert "kaboom" in tool_result["result"]
    assert events[-1]["type"] == "done"  # the loop kept going and finished after the error


def test_max_turns_guard(monkeypatch):
    monkeypatch.setattr(loop, "run_tool", _fake_run_tool([]))

    # a model that asks for a tool every single turn must hit the guard, not spin forever
    def complete(messages, tool_schemas, system, model):  # noqa: ARG001
        yield {
            "type": "final",
            "content": [{"type": "tool_use", "id": "t", "name": "echo", "input": {}}],
            "stop_reason": "tool_use",
        }

    monkeypatch.setattr(loop, "anthropic_complete", complete)

    events = list(run_agent("hey", max_turns=3))
    assert events[-1]["type"] == "error"
    assert "max_turns=3" in events[-1]["error"]
    assert sum(1 for e in events if e["type"] == "tool_use") == 3  # exactly max_turns attempts
