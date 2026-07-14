"""The ReAct loop. This is the whole product; the CLI and the SSE server are just transports.

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

from common.env import load_local_env

from .tools import Toolbox, default_toolbox

load_local_env()  # so ANTHROPIC_API_KEY from the root .env is present before Anthropic() reads it

DEFAULT_MODEL = os.environ.get("CHAT_MODEL", "claude-sonnet-5")
MAX_TOKENS = 1024

SYSTEM = (
    "You are the sysdesign assistant, a data agent over Defrag's influencer-intelligence system. "
    "Use the tools to answer questions about tracked creators, their scraped signals, the AI "
    "relevance ratings, background scrape runs, and the weekly digests. When the user asks you to "
    "DO something, like start a scrape, call the tool and then report what happened, including any "
    "id the caller can follow up with. Default runs to demo mode unless the user asks for live. "
    "Keep answers short and concrete."
)

# complete(messages, tool_schemas, system, model) -> yields {"type":"text_delta","text":..}
# events, then exactly one {"type":"final","content":[blocks],"stop_reason":..}. Blocks are plain
# dicts: {"type":"text","text":..} or {"type":"tool_use","id":..,"name":..,"input":..}.
Complete = Callable[[list[dict], list[dict], str, str], Iterator[dict]]


def anthropic_complete(messages: list[dict], tool_schemas: list[dict], system: str, model: str) -> Iterator[dict]:
    """The real model call: one streamed turn off the Anthropic SDK. Yields text deltas as they
    arrive (that's the token streaming the UI shows), then normalizes the final message's content
    blocks into plain dicts so the rest of the loop never touches SDK objects."""
    from anthropic import Anthropic

    client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    with client.messages.stream(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        tools=tool_schemas,
        messages=messages,
    ) as stream:
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


def run_agent(
    user_message: str,
    *,
    complete: Complete | None = None,
    toolbox: Toolbox | None = None,
    system: str = SYSTEM,
    model: str = DEFAULT_MODEL,
    max_turns: int = 8,
    history: list[dict] | None = None,
) -> Iterator[dict]:
    """Run one user turn to completion, yielding a flat stream of events the transports render:

      {"type":"text","text":..}                              a streamed answer token
      {"type":"tool_use","id":..,"name":..,"input":..}       the model asked to call a tool
      {"type":"tool_result","id":..,"name":..,"ok":bool,"result":..}   what the tool returned
      {"type":"done","stop_reason":..,"turns":n}             terminal, the answer is complete
      {"type":"error","error":..}                            no final message, or max_turns hit

    Pass `history` (prior [{"role","content"}] messages) to continue a multi-turn chat.
    """
    complete = complete or anthropic_complete
    toolbox = toolbox if toolbox is not None else default_toolbox()
    schemas = toolbox.schemas

    messages: list[dict] = list(history or [])
    messages.append({"role": "user", "content": user_message})

    for turn in range(1, max_turns + 1):
        final: dict | None = None
        for ev in complete(messages, schemas, system, model):
            if ev["type"] == "text_delta":
                yield {"type": "text", "text": ev["text"]}
            elif ev["type"] == "final":
                final = ev

        if final is None:  # a well-behaved complete always yields a final; guard anyway
            yield {"type": "error", "error": "model produced no final message"}
            return

        messages.append({"role": "assistant", "content": final["content"]})

        if final["stop_reason"] != "tool_use":
            yield {"type": "done", "stop_reason": final["stop_reason"], "turns": turn}
            return

        tool_results: list[dict] = []
        for block in final["content"]:
            if block["type"] != "tool_use":
                continue
            yield {"type": "tool_use", "id": block["id"], "name": block["name"], "input": block["input"]}
            try:
                result: Any = toolbox.run(block["name"], block["input"])
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
