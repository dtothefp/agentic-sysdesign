# Build the agent from scratch

This branch (`learn/agent-from-scratch`) hollows out the Module 7 chat agent so you can rebuild
the interesting parts yourself and feel how the async pieces fit. The scaffolding, types, tool
framework, auth gate, and tests are left intact. Three things are stubbed to
`raise NotImplementedError`, in the order you should build them:

1. `agent/loop.py` -> `run_agent` (the ReAct loop) and `anthropic_complete` (the streamed model call)
2. `agent/tools.py` -> seven HTTP tool functions (one, `search_signals`, is left as a worked example)
3. `agent/server.py` -> the `/chat` SSE handler (the sync-generator -> async-stream bridge)

Everything else (`Tool`/`Toolbox`, `DEFAULT_TOOLS`, `_trace.py`, `__main__.py`, the `/health` route,
the X-API-Key gate, `pyproject.toml`, `railway.json`) is untouched.

## The one mental model

A ReAct agent is a `while` loop around a model call:

```
seed transcript with the user's message
loop (up to max_turns):
    ask the model, streaming its text out as it arrives
    it either ANSWERS (stop_reason "end_turn")  -> emit done, stop
    or it asks for TOOLS (stop_reason "tool_use") -> run each tool,
        append the results as the next user turn, loop again
loop fell through -> emit an error (the model never stopped asking for tools)
```

The model never runs your code. It emits `tool_use` blocks (name + JSON input); YOU execute them
and feed the results back. That round-trip is the whole trick. The loop only ever sees plain dicts,
never Anthropic SDK objects, that seam (`Complete`) is what lets the tests drive it with a scripted
model and no network.

## Order of work

### 1. `run_agent` in `loop.py` (start here)

The unit tests are the spec. Run them red first:

```bash
uv run --package sysdesign-agent pytest tests/test_loop.py -x
```

Three tests: a tool call then an answer, a tool that raises (must NOT kill the turn, you feed the
error back as an `is_error` tool_result), and the `max_turns` guard. The stub's docstring lists the
exact event shapes and the append/loop steps. `run_agent` is a **generator**, you `yield` events
one at a time rather than returning a list. When all three pass, the loop mechanics are done.

Docs:
- Python generators / `yield`: https://docs.python.org/3/howto/functional.html#generators
- Anthropic tool use (what `tool_use` / `tool_result` blocks look like on the wire):
  https://docs.claude.com/en/docs/agents-and-tools/tool-use/overview

### 2. `anthropic_complete` in `loop.py`

No test covers this (the tests inject a fake `complete`), so you verify it live. It's one streamed
`client.messages.stream(...)` call: yield a `text_delta` per token off `stream.text_stream`, then
normalize `stream.get_final_message().content` into plain dicts and yield one `final`. The stub
docstring has the five steps.

Docs:
- Streaming messages: https://docs.claude.com/en/api/messages-streaming
- Messages API reference: https://docs.claude.com/en/api/messages
- Python SDK (streaming helpers, `messages.stream`, `get_final_message`):
  https://github.com/anthropics/anthropic-sdk-python

Check it end to end from the CLI (needs `ANTHROPIC_API_KEY` in the workspace-root `.env`, and the
data API running, `moon run api:dev`, or point at prod):

```bash
# local api:
uv run --package sysdesign-agent python -m agent "what creators do we track?"
# or against prod:
SYSDESIGN_API_URL=https://sysdesign.thedefrag.ai uv run --package sysdesign-agent python -m agent "what are creators saying about MCP?"
```

### 3. The seven tools in `tools.py`

Each is one HTTP call. Copy the `search_signals` worked example: build a client with `_client()`,
GET (or POST, for `trigger_run`) the endpoint named in the stub's docstring, `raise_for_status()`,
return `.json()`. The endpoints are the services/api surface, browse them at
`http://localhost:8000/docs` once the api is running. A tool that raises is fine, the loop turns it
into an `is_error` result, so you can build them one at a time.

### 4. The `/chat` SSE bridge in `server.py`

`run_agent` is a **sync** generator, but the FastAPI handler is **async**. Iterating the generator
directly would block the event loop for the entire model call. The fix is to pull each event on a
thread with `await asyncio.to_thread(next, it, sentinel)`. The stub docstring has the exact shape.
The auth tests (`tests/test_server_auth.py`) already pass, they never reach this handler.

Docs:
- `asyncio.to_thread`: https://docs.python.org/3/library/asyncio-task.html#asyncio.to_thread
- Async generators (PEP 525 background): https://peps.python.org/pep-0525/
- sse-starlette `EventSourceResponse`: https://github.com/sysid/sse-starlette

Run the server and hit it:

```bash
moon run agent:dev   # uvicorn on :8100
curl -N localhost:8100/chat -H 'Content-Type: application/json' -d '{"message":"what are creators saying about MCP?"}'
```

## Definition of done

- `uv run --package sysdesign-agent pytest tests` is green (loop + auth).
- `python -m agent "..."` streams tokens, calls tools, prints a `[done: end_turn ...]` line.
- `moon run agent:dev` + the curl above streams `text` / `tool_use` / `tool_result` / `done` events.
- `moon run agent:lint` is clean.

The full reference implementation is on `main` if you want to diff your version against it after.
There is also a parallel **TypeScript** rebuild of this same agent on `feat/ts-chat-service`, the
loop is an async generator there (no thread bridge needed), which is a nice contrast to study once
you've done the Python version.
