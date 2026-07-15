"""LangSmith tracing, soft-imported and inert-until-keyed.

Two seams the loop uses:

  * `wrap_anthropic(client)` patches the Anthropic client so every `messages.stream` call becomes
    a first-class LLM span in LangSmith, with the system prompt, the running message list, the
    tool schemas we hand the model, its streamed response, the tool_use blocks it chose, and token
    usage all captured. That's the "see the prompts and tool calls" view.
  * `@traceable(...)` wraps our own functions (the agent turn, each tool call) so they nest under
    the LLM spans as a trace tree.

Both are soft-imported so a venv without `langsmith` still runs (the decorator degrades to a
passthrough, the wrapper to identity), and even when installed they emit nothing until
LANGSMITH_TRACING=true and a LANGSMITH_API_KEY are in the env, the same inert-until-keyed contract
the Module 4 rating and Module 6 embedding layers use. This mirrors those layers' idiom on
purpose; the agent is a client of the API, and its observability stays optional and off the loop's
own logic. LangSmith is a tracer, not an LLM SDK, so this does not add LangChain/LangGraph.
"""

from __future__ import annotations

from typing import Any

try:
    from langsmith import traceable
    from langsmith.wrappers import wrap_anthropic
except ImportError:  # pragma: no cover - tracing is optional

    def traceable(*d_args: Any, **d_kwargs: Any):  # type: ignore[misc]
        # support both bare @traceable and @traceable(run_type=.., name=.., ...)
        if len(d_args) == 1 and callable(d_args[0]) and not d_kwargs:
            return d_args[0]
        return lambda fn: fn

    def wrap_anthropic(client: Any, *args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
        return client


__all__ = ["traceable", "wrap_anthropic"]
