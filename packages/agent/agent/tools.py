"""The agent's tool layer: thin, synchronous HTTP clients of the sysdesign REST API.

Design choice worth saying out loud (it's the interview point): the agent is a CLIENT of the
data API, not code fused into it. Every tool is one `httpx` call to services/api, authenticated
by the same X-API-Key the rest of the app uses. That means the exact same loop can point at a
local api (SYSDESIGN_API_URL=http://localhost:8000) or the deployed one
(https://sysdesign.thedefrag.ai) by changing one env var, and the tools never grow a second
in-process code path. The loop in loop.py doesn't know or care that the tools are HTTP.

A tool is (name, description, JSON-Schema for its inputs, a callable). The Toolbox turns that
list into the two things the loop needs: the schemas to hand the model, and a `run(name, input)`
dispatch. Anything the callable returns must be JSON-serializable, because it goes straight back
to the model as a tool_result.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from common.env import load_local_env

load_local_env()  # populate os.environ from the workspace-root .env (env-first, never overrides)

API_URL = os.environ.get("SYSDESIGN_API_URL", "http://localhost:8000")
API_KEY = os.environ.get("SYSDESIGN_API_KEY")


def _client() -> httpx.Client:
    """One short-lived client per tool call. The API key rides the X-API-Key header when the
    deployment is keyed; unset means the API is open (local dev), same inert-until-keyed contract
    the server enforces."""
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    return httpx.Client(base_url=API_URL, headers=headers, timeout=30.0)


# --- the tools -----------------------------------------------------------------
#
# Read tools (influencers, signals, ratings, digests) plus one action tool (trigger_run) and its
# status read (get_run). Enough surface for the agent to answer "what are we tracking / what's
# on-thesis lately" and to actually DO something ("kick off a scrape") and report the result.


def list_influencers() -> list[dict]:
    """The creators on Defrag's watchlist (name, handle, last_scraped_at)."""
    with _client() as c:
        r = c.get("/influencers")
        r.raise_for_status()
        return r.json()


def list_ratings(min_relevance: float | None = None, limit: int = 20) -> list[dict]:
    """Recent per-signal AI ratings, newest first. min_relevance filters to what the model
    judged on-thesis for Defrag's AI-research angle (0 to 1)."""
    params: dict[str, Any] = {"limit": limit}
    if min_relevance is not None:
        params["min_relevance"] = min_relevance
    with _client() as c:
        r = c.get("/ratings", params=params)
        r.raise_for_status()
        return r.json()


def list_recent_signals(hours: int = 24, influencer_id: int | None = None, limit: int = 50) -> list[dict]:
    """Raw signals captured in the last `hours` hours. The API requires a time window (it's the
    partition key), so this computes now-minus-hours to now and passes it through."""
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
    """Kick off a fan-out scrape run. mode is 'demo' (synthetic signals, no scraper spend) or
    'live' (real Apify scrape). Optional model ('provider/model') turns on AI rating for the run.
    Returns the run_id immediately; the work happens in the background, poll it with get_run."""
    body: dict[str, Any] = {"mode": mode}
    if model is not None:
        body["model"] = model
    with _client() as c:
        r = c.post("/runs", json=body)
        r.raise_for_status()
        return r.json()


def get_run(run_id: int) -> dict:
    """Current state of one run (status, done_count, total, rated_count, timestamps)."""
    with _client() as c:
        r = c.get(f"/runs/{run_id}")
        r.raise_for_status()
        return r.json()


def list_digests(limit: int = 5) -> list[dict]:
    """Recent agent-written weekly digests (id, status, word_count, created_at). Fetch the body
    of one via get_digest so the model doesn't pull every digest's full markdown at once."""
    with _client() as c:
        r = c.get("/digests", params={"limit": limit})
        r.raise_for_status()
        return r.json()


def get_digest(digest_id: int) -> dict:
    """One digest including its markdown content, once the digest agent has delivered it."""
    with _client() as c:
        r = c.get(f"/digests/{digest_id}")
        r.raise_for_status()
        return r.json()


def search_signals(q: str, limit: int = 20) -> dict:
    """Find signals by what they're ABOUT, not by time or score. The Module 6 hybrid search:
    Postgres full-text plus pgvector, fused with RRF. This is how the chat agent answers content
    questions ('what have creators said about MCP?', 'any posts on evals?') that the time-windowed
    and rating-filtered lists can't. Each hit reports which halves found it and a fused score; the
    response's `semantic` flag is false when no embedding model is keyed (lexical-only, still useful)."""
    with _client() as c:
        r = c.get("/search", params={"q": q, "limit": limit})
        r.raise_for_status()
        return r.json()


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: Callable[..., Any]


class Toolbox:
    """Indexes a list of Tools into what the loop needs: `.schemas` for the model, `.run` for
    dispatch. Kept model-agnostic (plain dict schemas) so the loop is trivially unit-testable
    with a fake toolbox."""

    def __init__(self, tools: list[Tool]):
        self._by_name = {t.name: t for t in tools}

    @property
    def schemas(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in self._by_name.values()]

    def run(self, name: str, tool_input: dict | None) -> Any:
        tool = self._by_name.get(name)
        if tool is None:
            raise KeyError(f"unknown tool {name!r}")
        return tool.fn(**(tool_input or {}))


DEFAULT_TOOLS: list[Tool] = [
    Tool(
        "list_influencers",
        "List the creators on Defrag's watchlist (name, instagram handle, last_scraped_at).",
        {"type": "object", "properties": {}},
        list_influencers,
    ),
    Tool(
        "list_ratings",
        "List recent per-signal AI relevance ratings, newest first. Use min_relevance to see only on-thesis signals.",
        {
            "type": "object",
            "properties": {
                "min_relevance": {"type": "number", "minimum": 0, "maximum": 1, "description": "minimum relevance 0-1"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "max rows (default 20)"},
            },
        },
        list_ratings,
    ),
    Tool(
        "list_recent_signals",
        "List raw scraped signals captured in the last N hours (optionally for one influencer_id).",
        {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "minimum": 1, "description": "look-back window in hours (default 24)"},
                "influencer_id": {"type": "integer", "description": "restrict to one creator"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "max rows (default 50)"},
            },
        },
        list_recent_signals,
    ),
    Tool(
        "trigger_run",
        "Kick off a background scrape run. mode 'demo' (synthetic, free) or 'live' (real scrape). "
        "Optional model ('provider/model') enables AI rating.",
        {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["demo", "live"], "description": "default demo"},
                "model": {"type": "string", "description": "e.g. 'deepseek/deepseek-chat'; omit to skip rating"},
            },
        },
        trigger_run,
    ),
    Tool(
        "get_run",
        "Get the current state of one run by id (status, done_count/total, rated_count).",
        {"type": "object", "properties": {"run_id": {"type": "integer"}}, "required": ["run_id"]},
        get_run,
    ),
    Tool(
        "list_digests",
        "List recent agent-written weekly digests (id, status, word_count, created_at).",
        {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}}},
        list_digests,
    ),
    Tool(
        "get_digest",
        "Get one digest including its markdown content by id.",
        {"type": "object", "properties": {"digest_id": {"type": "integer"}}, "required": ["digest_id"]},
        get_digest,
    ),
    Tool(
        "search_signals",
        "Search signals by topic/content (hybrid full-text + semantic). Use this for 'what did "
        "creators say about X' questions the time-windowed and rating lists can't answer.",
        {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "free-text query; supports quoted phrases and -negation"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "max hits (default 20)"},
            },
            "required": ["q"],
        },
        search_signals,
    ),
]


def default_toolbox() -> Toolbox:
    """The production toolbox: every tool wired to the REST API at SYSDESIGN_API_URL."""
    return Toolbox(DEFAULT_TOOLS)
