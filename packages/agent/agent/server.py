"""SSE transport: a tiny FastAPI app that streams the agent loop to a browser.

This is the bridge to the future chat-web (Next.js) UI. It's a separate process from
services/api on purpose, because the agent is a CLIENT of the data API, not part of it: it gets
its own /chat surface on its own port (8100) and dials the api over HTTP like any other client.

The one interesting bit is the sync/async bridge. run_agent is a SYNC generator (sync Anthropic +
sync httpx tools). Iterating it directly from an async handler would block the event loop for the
whole model call, so instead we pull each event in a worker thread via asyncio.to_thread(next, ..).
Only one pull runs at a time, so the generator's state is safe, and the event loop stays free to
serve other requests and to notice client disconnects. Same EventSourceResponse primitive the
api's run/digest streams use, so the wire format is identical: one SSE event per loop event.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .loop import run_agent

app = FastAPI(title="sysdesign chat agent", version="0.1.0")


class ChatIn(BaseModel):
    message: str
    history: list[dict] | None = None  # prior [{"role","content"}] turns, to continue a chat


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/chat")
async def chat(body: ChatIn):
    """Stream one agent turn as SSE. Each event's `event:` field is the loop event type
    (text, tool_use, tool_result, done, error) and `data:` is the JSON event."""

    def _events():
        return run_agent(body.message, history=body.history)

    async def gen():
        it = await asyncio.to_thread(_events)
        sentinel = object()
        while True:
            ev = await asyncio.to_thread(next, it, sentinel)
            if ev is sentinel:
                break
            yield {"event": ev["type"], "data": json.dumps(ev, default=str)}

    return EventSourceResponse(gen())
