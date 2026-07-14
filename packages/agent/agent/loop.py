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

import os
from collections.abc import Callable, Iterator

from common.env import load_local_env

from ._trace import traceable, wrap_anthropic  # noqa: F401 - wrap_anthropic is for the anthropic_complete you build
from .tools import Toolbox, default_toolbox  # noqa: F401 - default_toolbox is for the run_agent you build

load_local_env()  # so ANTHROPIC_API_KEY (and the LANGSMITH_* tracing vars) from the root .env are
# present before Anthropic() and the first traced call read them

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


def anthropic_complete(messages: list[dict], tool_schemas: list[dict], system: str, model: str) -> Iterator[dict]:
    """The real model call: one streamed turn off the Anthropic SDK.

    >>> BUILD THIS. Nothing in the unit tests calls it (they inject a scripted `complete`), so it
    >>> has no test to satisfy; you exercise it by running `python -m agent "..."` against a live
    >>> api. Match the `Complete` contract exactly so run_agent stays model-agnostic.

    What it must do:
      1. Create an Anthropic client: `client = wrap_anthropic(Anthropic())`. The wrap_anthropic
         seam turns the streamed call into a LangSmith span when tracing is on, and is a no-op
         otherwise (see _trace.py). Anthropic() reads ANTHROPIC_API_KEY from the environment.
      2. Open a streaming turn: `with client.messages.stream(model=, max_tokens=MAX_TOKENS,
         system=, tools=tool_schemas, messages=messages) as stream:`.
      3. For each text token off `stream.text_stream`, yield {"type":"text_delta","text": token}.
         (That yield is what makes tokens appear live in the CLI and the SSE UI.)
      4. After the stream is exhausted, get the final message (`stream.get_final_message()`) and
         normalize its `.content` blocks into PLAIN DICTS so the rest of the loop never touches
         SDK objects: a text block -> {"type":"text","text": block.text}, a tool_use block ->
         {"type":"tool_use","id": block.id, "name": block.name, "input": block.input}.
      5. yield exactly one {"type":"final","content": <those dicts>, "stop_reason": final.stop_reason}.

    Docs: https://docs.claude.com/en/api/messages-streaming  and the tool-use guide
    https://docs.claude.com/en/docs/agents-and-tools/tool-use/overview
    """
    raise NotImplementedError("build anthropic_complete: stream a turn, yield text_delta events, then one final")


def _turn_inputs(inputs: dict) -> dict:
    """What LangSmith shows as this turn's input. Keep it to the user's message and any prior
    history; drop the injected seams (complete, toolbox, schemas, model) that would otherwise dump
    the whole tool catalog and a function repr into the trace."""
    return {"user_message": inputs.get("user_message"), "history": inputs.get("history")}


@traceable(run_type="chain", name="agent_turn", process_inputs=_turn_inputs)
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
    raise NotImplementedError("build run_agent: the ReAct loop. Make tests/test_loop.py green.")
