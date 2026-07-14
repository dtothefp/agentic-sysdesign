"""The ReAct loop. This is the whole product; the CLI and the SSE server are just transports.

>>> STRIPPED FOR STUDY. The two functions below are hollowed out to `raise NotImplementedError`.
>>> Your job is to fill them in. The unit tests in tests/test_loop.py are the spec: run them red,
>>> make them green. See BUILD_FROM_SCRATCH.md for the algorithm, the async bits, and doc links.

The mechanics, which are exactly what a code challenge pokes at:

  1. Send the running transcript plus the tool schemas to the model.
  2. If it returns a normal answer (stop_reason 'end_turn'), we're done.
  3. If it returns one or more tool_use blocks (stop_reason 'tool_use'), run each tool, append
     the results as a user turn, and loop. A tool that raises does NOT kill the turn: we hand the
     error back as a tool_result with is_error, so the model can apologize or try another path.
  4. A max_turns guard stops a model that keeps asking for tools forever (a real failure mode,
     not a theoretical one).

The model call is abstracted behind `complete`, a generator that yields text deltas then one
final message. The default implementation (`anthropic_complete`) streams real tokens off the
Anthropic SDK; tests inject a scripted `complete` and never touch the network. That seam is what
makes the loop unit-testable and keeps run_agent model-agnostic: it only ever sees plain dicts.

`run_agent` is a SYNC generator on purpose. The Anthropic client and the httpx tools are both
sync, so the loop stays linear and easy to read, and the async SSE server (server.py) pulls each
event in a worker thread rather than the loop pretending to be async.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from typing import Any

from anthropic import Anthropic
from common.env import load_local_env

from ._trace import traceable, wrap_anthropic  # noqa: F401 - wrap_anthropic is for the anthropic_complete you build
from .tools import TOOL_SCHEMAS, run_tool

load_local_env()  # so ANTHROPIC_API_KEY (and the LANGSMITH_* tracing vars) from the root .env are
# present before Anthropic() and the first traced call read them

_anthropic_client = wrap_anthropic(Anthropic())  # one client per process, reused across turns

DEFAULT_MODEL = os.environ.get("CHAT_MODEL", "claude-sonnet-5")
MAX_TOKENS = 4096  # cap on the model's output per turn. Big enough that a long summary isn't
# truncated mid-sentence (a real bug the Python prod agent hit at 1024). Interview note: this is
# an OUTPUT cap, not the context window.

SYSTEM = (
    "You are the sysdesign assistant, a data agent over Defrag's influencer-intelligence system. "
    "Use the tools to answer questions about tracked creators, their scraped signals, the AI "
    "relevance ratings, background scrape runs, and the weekly digests. For questions about what "
    "creators have said on a TOPIC (rather than by time or rating), use search_signals. When the user asks you to "
    "DO something, like start a scrape, call the tool and then report what happened, including any "
    "id the caller can follow up with. Default runs to demo mode unless the user asks for live. "
    "Keep answers short and concrete."
)

# complete(messages, tool_schemas, system, model) -> yields {"type":"text_delta","text":..}
# events, then exactly one {"type":"final","content":[blocks],"stop_reason":..}. Blocks are plain
# dicts: {"type":"text","text":..} or {"type":"tool_use","id":..,"name":..,"input":..}.
Complete = Callable[[list[dict], list[dict], str, str], Iterator[dict]]


def simple_complete(
    messages: list[dict],
    tool_schemas: list[dict],  # noqa: ARG001 - real completes pass tool schemas to the model
    system: str,  # noqa: ARG001
    model: str,  # noqa: ARG001
) -> Iterator[dict]:
    """Toy `complete` for learning the contract. No Anthropic client, no network.

    `complete` means "one model turn": stream some tokens, then hand back one final message.
    `run_agent` calls it once per loop iteration and owns the transcript (messages list).

    Try it in a REPL:
        list(simple_complete([{"role": "user", "content": "hello"}], [], SYSTEM, "fake"))
    """
    last = messages[-1]["content"] if messages else ""
    if isinstance(last, list):
        # After a tool round, Anthropic-shaped transcripts carry tool_result blocks here.
        reply = "Thanks, I saw your tool results."
    else:
        reply = f"You said: {last}"

    for word in reply.split():
        yield {"type": "text_delta", "text": word + " "}

    yield {
        "type": "final",
        "content": [{"type": "text", "text": reply}],
        "stop_reason": "end_turn",
    }


def anthropic_complete(messages: list[dict], tool_schemas: list[dict], system: str, model: str) -> Iterator[dict]:
    """One streamed model turn via the Anthropic SDK. No tools until tool_schemas is non-empty."""
    stream_kwargs: dict = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": messages,
    }
    if tool_schemas:
        stream_kwargs["tools"] = tool_schemas

    with _anthropic_client.messages.stream(**stream_kwargs) as stream:
        for text in stream.text_stream:
            yield {"type": "text_delta", "text": text}
        final = stream.get_final_message()

    content: list[dict] = []
    for block in final.content:
        if block.type == "text":
            content.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            content.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})

    yield {"type": "final", "content": content, "stop_reason": final.stop_reason}


def _turn_inputs(inputs: dict) -> dict:
    """What LangSmith shows as this turn's input. Keep it to the user's message and any prior
    history; drop the injected seams (complete, toolbox, schemas, model) that would otherwise dump
    the whole tool catalog and a function repr into the trace."""
    return {"user_message": inputs.get("user_message"), "history": inputs.get("history")}


@traceable(run_type="chain", name="agent_turn", process_inputs=_turn_inputs)
def run_agent(
    user_message: str,
    *,
    system: str = SYSTEM,
    model: str = DEFAULT_MODEL,
    max_turns: int = 8,
    history: list[dict] | None = None,
) -> Iterator[dict]:
    """Run one user turn to completion, yielding a flat stream of events the transports render:

      {"type":"text","text":..}                              a streamed answer token
      {"type":"tool_use","id":..,"name":..,"input":..}       the model asked to call a tool
      {"type":"tool_result","id":..,"name":..,"ok":bool,"result":..}   what the tool returned
      {"type":"final","content":[..],"stop_reason":..}        full assistant message for this turn
      {"type":"done","stop_reason":..,"turns":n}             terminal, the answer is complete
      {"type":"error","error":..}                            no final message, or max_turns hit

    Pass `history` (prior [{"role","content"}] messages) to continue a multi-turn chat.

    >>> BUILD THIS. tests/test_loop.py is the spec. The shape it asserts:
    >>>   * default the seams: complete = complete or anthropic_complete;
    >>>     toolbox = toolbox if toolbox is not None else default_toolbox(); schemas = toolbox.schemas
    >>>   * seed the transcript: messages = list(history or []) + one {"role":"user","content":user_message}
    >>>   * loop `for turn in range(1, max_turns + 1)`:
    >>>       - drive `complete(messages, schemas, system, model)`: re-yield each text_delta as a
    >>>         {"type":"text",...} event, and capture the single {"type":"final",...}
    >>>       - if no final came back, yield an error event and return
    >>>       - append the assistant turn: messages.append({"role":"assistant","content": final["content"]})
    >>>       - if stop_reason != "tool_use": yield the done event {stop_reason, turns:turn} and return
    >>>       - else for each tool_use block: yield a tool_use event, run it through the toolbox
    >>>         (toolbox.run(name, input)), catch ANY exception into an is_error result so one bad
    >>>         tool can't kill the turn, yield a tool_result event, and collect an Anthropic
    >>>         tool_result block {"type":"tool_result","tool_use_id":id,"content": json.dumps(result),
    >>>         "is_error": not ok}. After the blocks, append them as ONE user turn.
    >>>   * fell out of the loop => yield an error event: f"hit max_turns={max_turns} without a final answer"
    >>>
    >>> The three tests to make green: tool-call-then-answer, tool-error-is-recoverable, max_turns_guard.
    >>> Python generators + `yield`: https://docs.python.org/3/howto/functional.html#generators
    """
    messages = list(history or []) + [{"role": "user", "content": user_message}]

    for turn in range(1, max_turns + 1):
        final: dict | None = None
        for ev in anthropic_complete(messages, TOOL_SCHEMAS, system, model):
            if ev["type"] == "text_delta":
                yield {"type": "text", "text": ev["text"]}
            elif ev["type"] == "final":
                final = ev

        if final is None:
            yield {"type": "error", "error": "model produced no final message"}
            return

        messages.append({"role": "assistant", "content": final["content"]})

        if final["stop_reason"] != "tool_use":
            yield {"type": "final", "content": final["content"], "stop_reason": final["stop_reason"]}
            yield {"type": "done", "stop_reason": final["stop_reason"], "turns": turn}
            return

        tool_results: list[dict] = []
        for block in final["content"]:
            if block["type"] != "tool_use":
                continue
            yield {"type": "tool_use", "id": block["id"], "name": block["name"], "input": block["input"]}
            try:
                result: Any = run_tool(block["name"], block["input"])
                ok = True
            except Exception as e:  # noqa: BLE001 - a failing tool must be recoverable, not fatal
                result = f"{type(e).__name__}: {e}"
                ok = False
            yield {"type": "tool_result", "id": block["id"], "name": block["name"], "ok": ok, "result": result}
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": json.dumps(result, default=str),
                    "is_error": not ok,
                }
            )
        messages.append({"role": "user", "content": tool_results})

    yield {"type": "error", "error": f"hit max_turns={max_turns} without a final answer"}
