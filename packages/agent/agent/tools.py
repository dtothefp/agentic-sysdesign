"""Thin HTTP tools the agent calls against services/api.

Each tool is a Tool dataclass (name, description, input_schema, fn) registered in TOOLS.
TOOL_SCHEMAS is derived for Anthropic. run_tool dispatches via LangSmith-traced callables.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from common.env import load_local_env

from ._trace import traceable

load_local_env()

API_URL = os.environ.get("SYSDESIGN_API_URL", "http://localhost:8000")
API_KEY = os.environ.get("SYSDESIGN_API_KEY")


@dataclass(frozen=True)
class Tool:
    """Bundles the four pieces of one tool. frozen=True means it can't be mutated after creation.

    Like a typed object in JS/TS instead of a loose dict:
      { name, description, input_schema, fn }
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., Any]


def _client() -> httpx.Client:
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    return httpx.Client(base_url=API_URL, headers=headers, timeout=30.0)


def list_influencers() -> list[dict]:
    """GET /influencers — the creators on Defrag's watchlist."""
    with _client() as c:
        r = c.get("/influencers")
        r.raise_for_status()
        return r.json()


def list_ratings(min_relevance: float | None = None, limit: int = 20) -> list[dict]:
    """GET /ratings — recent per-signal AI ratings, newest first."""
    params: dict[str, Any] = {"limit": limit}
    if min_relevance is not None:
        params["min_relevance"] = min_relevance
    with _client() as c:
        r = c.get("/ratings", params=params)
        r.raise_for_status()
        return r.json()


def list_recent_signals(hours: int = 24, influencer_id: int | None = None, limit: int = 50) -> list[dict]:
    """GET /signals — raw signals in the last `hours` (API requires a time window)."""
    now = datetime.now(UTC)
    params: dict[str, Any] = {
        "from": (now - timedelta(hours=hours)).isoformat(),
        "to": now.isoformat(),
        "limit": limit,
    }
    if influencer_id is not None:
        params["influencer_id"] = influencer_id
    with _client() as c:
        r = c.get("/signals", params=params)
        r.raise_for_status()
        return r.json()


def trigger_run(mode: str = "demo", model: str | None = None) -> dict:
    """POST /runs — kick off a background scrape run (demo or live)."""
    body: dict[str, Any] = {"mode": mode}
    if model is not None:
        body["model"] = model
    with _client() as c:
        r = c.post("/runs", json=body)
        r.raise_for_status()
        return r.json()


def get_run(run_id: int) -> dict:
    """GET /runs/{run_id} — current state of one scrape run."""
    with _client() as c:
        r = c.get(f"/runs/{run_id}")
        r.raise_for_status()
        return r.json()


def list_digests(limit: int = 5) -> list[dict]:
    """GET /digests — recent weekly digest summaries (no full markdown)."""
    with _client() as c:
        r = c.get("/digests", params={"limit": limit})
        r.raise_for_status()
        return r.json()


def get_digest(digest_id: int) -> dict:
    """GET /digests/{digest_id} — one digest including markdown content."""
    with _client() as c:
        r = c.get(f"/digests/{digest_id}")
        r.raise_for_status()
        return r.json()


def search_signals(q: str, limit: int = 20) -> dict:
    """GET /search — hybrid full-text + semantic search over signal captions."""
    with _client() as c:
        r = c.get("/search", params={"q": q, "limit": limit})
        r.raise_for_status()
        return r.json()


TOOLS: dict[str, Tool] = {
    "list_influencers": Tool(
        name="list_influencers",
        description="List the creators on Defrag's watchlist (name, instagram handle, last_scraped_at).",
        input_schema={"type": "object", "properties": {}},
        fn=list_influencers,
    ),
    "list_ratings": Tool(
        name="list_ratings",
        description=(
            "List recent per-signal AI relevance ratings, newest first. Use min_relevance to see only on-thesis signals."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "min_relevance": {"type": "number", "minimum": 0, "maximum": 1, "description": "minimum relevance 0-1"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "max rows (default 20)"},
            },
        },
        fn=list_ratings,
    ),
    "list_recent_signals": Tool(
        name="list_recent_signals",
        description="List raw scraped signals captured in the last N hours (optionally for one influencer_id).",
        input_schema={
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "minimum": 1, "description": "look-back window in hours (default 24)"},
                "influencer_id": {"type": "integer", "description": "restrict to one creator"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "max rows (default 50)"},
            },
        },
        fn=list_recent_signals,
    ),
    "trigger_run": Tool(
        name="trigger_run",
        description=(
            "Kick off a background scrape run. mode 'demo' (synthetic, free) or 'live' (real scrape). "
            "Optional model ('provider/model') enables AI rating."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["demo", "live"], "description": "default demo"},
                "model": {"type": "string", "description": "e.g. 'deepseek/deepseek-chat'; omit to skip rating"},
            },
        },
        fn=trigger_run,
    ),
    "get_run": Tool(
        name="get_run",
        description="Get the current state of one run by id (status, done_count/total, rated_count).",
        input_schema={
            "type": "object",
            "properties": {"run_id": {"type": "integer"}},
            "required": ["run_id"],
        },
        fn=get_run,
    ),
    "list_digests": Tool(
        name="list_digests",
        description="List recent agent-written weekly digests (id, status, word_count, created_at).",
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        },
        fn=list_digests,
    ),
    "get_digest": Tool(
        name="get_digest",
        description="Get one digest including its markdown content by id.",
        input_schema={
            "type": "object",
            "properties": {"digest_id": {"type": "integer"}},
            "required": ["digest_id"],
        },
        fn=get_digest,
    ),
    "search_signals": Tool(
        name="search_signals",
        description=(
            "Search signals by topic/content (hybrid full-text + semantic). Use for 'what did "
            "creators say about X' questions the time-windowed and rating lists can't answer."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "free-text query; supports quoted phrases and -negation"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "max hits (default 20)"},
            },
            "required": ["q"],
        },
        fn=search_signals,
    ),
}

# LangSmith tool spans (no-op when LANGSMITH_TRACING is off). One wrapped callable per tool.
_TRACED: dict[str, Callable[..., Any]] = {name: traceable(run_type="tool", name=name)(tool.fn) for name, tool in TOOLS.items()}

# Anthropic's tool catalog (no fn — the model never runs your code).
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema} for tool in TOOLS.values()
]


def run_tool(name: str, tool_input: dict[str, Any] | None) -> Any:
    """Look up the tool by name and call its trace-wrapped function with the model's input args."""
    fn = _TRACED.get(name)
    if fn is None:
        raise KeyError(f"unknown tool {name!r}")
    return fn(**(tool_input or {}))
