"""FastAPI surface over the Module 1 schema.

Deliberately thin. Two things it's built to demonstrate, because they're the interview
points the schema exists to make:

  1. Every write is the idempotent `INSERT ... ON CONFLICT DO NOTHING` upsert. The API
     computes content_hash server-side (one place, so clients can't disagree), so re-POSTing
     the identical signal is a no-op. That's the at-least-once story.
  2. The signals read REQUIRES an influencer + time window, so it always carries the
     partition key and prunes to the relevant month(s) instead of fanning across all of them.

The domain is Defrag's influencer watchlist: we track a set of creators (by Instagram
handle) and each scraped post/reel becomes a raw_signal. `last_scraped_at` on the influencer
is the incremental-scrape watermark, advanced via PATCH after each run.

Handlers are sync `def`, so FastAPI runs them in a threadpool and the sync psycopg pool
never blocks the event loop. One pool, opened at startup, closed at shutdown.
"""

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime

import psycopg
from common.db import DATABASE_URL
from common.rating import resolve_model
from common.search import embed_query, hybrid_search
from common.signals import insert_signal
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.security.api_key import APIKeyHeader
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from redis import asyncio as aioredis
from sse_starlette.sse import EventSourceResponse
from task_contract import DISPATCH_RUN, send_only_celery

from api.mcp_server import mcp as mcp_server
from api.models import (
    DailyRollup,
    Digest,
    DigestContent,
    Influencer,
    InfluencerIn,
    InfluencerWatermark,
    Rating,
    Run,
    RunCreated,
    RunTrigger,
    SearchHit,
    SearchResponse,
    Signal,
    SignalIn,
    SignalInsertResult,
    Source,
    SourceIn,
)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=False)

# Build the MCP ASGI app at import time; this is what lazily creates the session manager the
# lifespan below drives. Mounted at /mcp further down. (Module 5: the digest agent's tool surface.)
mcp_asgi = mcp_server.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The co-mounted MCP server needs its StreamableHTTP session manager running for the life of
    # the app, so nest it around the same pool open/close the API already did. One process, one
    # lifespan, both surfaces (REST + MCP) share this app's DATABASE_URL.
    async with mcp_server.session_manager.run():
        pool.open()
        yield
        pool.close()


# --- auth (Module 5) -------------------------------------------------------------
#
# One shared key on the X-API-Key header, enforced by a single app-level dependency.
# Same inert-until-keyed contract as the rating layer: SYSDESIGN_API_KEY unset means
# the API is open, set means every route below enforces it. /health stays open
# (Railway's healthcheck sends no headers). /openapi.json and /docs stay open too,
# they're Starlette-level routes that app dependencies never run on, and that's the
# point: the spec is the public discovery surface, and the security scheme declared
# in it (via Security(APIKeyHeader)) is how a client, or the Module 5 agent reading
# the spec, learns that data routes want a key.
#
# compare_digest instead of == so the check runs in constant time. A plain string
# compare bails at the first wrong byte, and that timing difference is measurable
# enough to leak a key byte-by-byte over a network (a timing attack).

API_KEY_HEADER = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,  # missing header -> None, we decide (else FastAPI 403s even when unkeyed)
    description="Required on data routes when the deployment sets SYSDESIGN_API_KEY.",
)


def require_api_key(request: Request, key: str | None = Security(API_KEY_HEADER)) -> None:
    expected = os.environ.get("SYSDESIGN_API_KEY")
    if not expected or request.url.path == "/health":
        return
    if key is None or not secrets.compare_digest(key, expected):
        raise HTTPException(401, "missing or invalid X-API-Key")


def _operation_id(route: APIRoute) -> str:
    # operationId == handler name, so a generated TypeScript client gets clean method names
    # (listSignals, createSignal) instead of FastAPI's default mangled ones. This is the one
    # tweak that makes the auto-generated OpenAPI spec pleasant to codegen against.
    return route.name


TAGS = [
    {"name": "health", "description": "Liveness + db reachability."},
    {"name": "influencers", "description": "The creators we track (Defrag watchlist)."},
    {"name": "sources", "description": "Where an influencer's signals come from."},
    {"name": "signals", "description": "The partitioned raw_signals firehose."},
    {"name": "rollup", "description": "Precomputed daily counts (materialized view)."},
    {"name": "runs", "description": "Background fan-out scrape jobs + live SSE progress."},
    {"name": "ratings", "description": "Per-signal AI ratings, keyed on content_hash."},
    {"name": "search", "description": "Module 6: hybrid lexical + semantic search over signal content, fused with RRF."},
    {"name": "digests", "description": "Weekly agent-written digests (Module 5). The agent delivers its own result via PUT."},
]

app = FastAPI(
    title="sysdesign API",
    version="0.2.0",
    description=(
        "Thin API over the Module 1 influencer-intelligence schema. FastAPI generates the "
        "OpenAPI spec automatically: machine-readable at /openapi.json, Swagger UI at /docs, "
        "ReDoc at /redoc. The Module 2 frontend codegens a typed client from that spec."
    ),
    openapi_tags=TAGS,
    lifespan=lifespan,
    generate_unique_id_function=_operation_id,
    dependencies=[Depends(require_api_key)],
)


# --- Module 5: the digest agent's MCP tool surface -------------------------------
#
# The remote MCP server (api/mcp_server.py) is co-mounted here so the agent can dial
# get_rated_signals directly, retiring the worker-held custom tool. App-level dependencies
# (require_api_key) don't run on a mounted sub-app, so the /mcp path gets its own bearer check,
# same inert-until-keyed SYSDESIGN_API_KEY contract as the REST routes. The vault's static_bearer
# credential injects that token at egress (packages/agents/vault/mcp-bearer.yaml); the sandbox never sees it.


@app.middleware("http")
async def _mcp_bearer_auth(request: Request, call_next):
    if request.url.path.startswith("/mcp"):
        expected = os.environ.get("SYSDESIGN_API_KEY")
        if expected:
            token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not token or not secrets.compare_digest(token, expected):
                return JSONResponse({"error": "missing or invalid bearer token"}, status_code=401)
    return await call_next(request)


app.mount("/mcp", mcp_asgi)


@app.get("/health", tags=["health"])
def health() -> dict:
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "db": "ok"}


# --- influencers ---------------------------------------------------------------


@app.get("/influencers", response_model=list[Influencer], tags=["influencers"])
def list_influencers():
    with pool.connection() as conn:
        return (
            conn.cursor(row_factory=dict_row)
            .execute("SELECT id, name, instagram_handle, last_scraped_at, created_at FROM influencers ORDER BY name")
            .fetchall()
        )


@app.post("/influencers", response_model=Influencer, tags=["influencers"])
def create_influencer(inf: InfluencerIn):
    # idempotent on the instagram_handle: re-adding a creator returns the existing row and
    # refreshes the display name. The scraper calls this so a handle only ever gets one row.
    with pool.connection() as conn:
        return (
            conn.cursor(row_factory=dict_row)
            .execute(
                "INSERT INTO influencers (name, instagram_handle) VALUES (%s, %s) "
                "ON CONFLICT (instagram_handle) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id, name, instagram_handle, last_scraped_at, created_at",
                (inf.name, inf.instagram_handle.lstrip("@").lower()),
            )
            .fetchone()
        )


@app.post("/influencers/bulk", response_model=list[Influencer], tags=["influencers"])
def create_influencers(infs: list[InfluencerIn]):
    """Seed the whole watchlist in one call. Same idempotent upsert as the single POST, run
    once per row inside a single transaction. This is how the scrape-signals skill loads the
    watchlist before scraping (POST the list, then GET it back for ids)."""
    with pool.connection() as conn:
        cur = conn.cursor(row_factory=dict_row)
        return [
            cur.execute(
                "INSERT INTO influencers (name, instagram_handle) VALUES (%s, %s) "
                "ON CONFLICT (instagram_handle) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id, name, instagram_handle, last_scraped_at, created_at",
                (inf.name, inf.instagram_handle.lstrip("@").lower()),
            ).fetchone()
            for inf in infs
        ]


@app.patch("/influencers/{influencer_id}", response_model=Influencer, tags=["influencers"])
def update_watermark(influencer_id: int, w: InfluencerWatermark):
    """Advance the incremental-scrape watermark. The scraper calls this after a run so the
    next run only pulls posts newer than last_scraped_at."""
    with pool.connection() as conn:
        row = (
            conn.cursor(row_factory=dict_row)
            .execute(
                "UPDATE influencers SET last_scraped_at = %s WHERE id = %s "
                "RETURNING id, name, instagram_handle, last_scraped_at, created_at",
                (w.last_scraped_at, influencer_id),
            )
            .fetchone()
        )
        if row is None:
            raise HTTPException(404, f"influencer {influencer_id} not found")
        return row


# --- sources -------------------------------------------------------------------


@app.get("/sources", response_model=list[Source], tags=["sources"])
def list_sources(influencer_id: int | None = None):
    sql = "SELECT id, influencer_id, kind, url, created_at FROM sources"
    params: list = []
    if influencer_id is not None:
        sql += " WHERE influencer_id = %s"
        params.append(influencer_id)
    sql += " ORDER BY id"
    with pool.connection() as conn:
        return conn.cursor(row_factory=dict_row).execute(sql, params).fetchall()


@app.post("/sources", response_model=Source, tags=["sources"])
def create_source(s: SourceIn):
    with pool.connection() as conn:
        try:
            return (
                conn.cursor(row_factory=dict_row)
                .execute(
                    "INSERT INTO sources (influencer_id, kind, url) VALUES (%s, %s, %s) "
                    "RETURNING id, influencer_id, kind, url, created_at",
                    (s.influencer_id, s.kind, s.url),
                )
                .fetchone()
            )
        except psycopg.errors.ForeignKeyViolation:
            raise HTTPException(400, f"influencer_id {s.influencer_id} does not exist") from None


# --- signals -------------------------------------------------------------------


@app.post("/signals", response_model=SignalInsertResult, tags=["signals"])
def create_signal(sig: SignalIn):
    """Idempotent write. content_hash is derived server-side, so identical payloads dedupe no
    matter who sends them. Returns inserted=False when the exact signal already existed. The
    actual upsert is common.signals.insert_signal, the same function the Celery worker calls,
    so there's literally one write path."""
    with pool.connection() as conn:
        try:
            inserted, h = insert_signal(conn, sig.influencer_id, sig.captured_at, sig.payload, sig.source_id)
        except psycopg.errors.ForeignKeyViolation:
            raise HTTPException(400, f"influencer_id {sig.influencer_id} does not exist") from None
        except psycopg.errors.CheckViolation as e:
            # no partition covers captured_at. Provision the month (create_month_partition)
            # or pick a captured_at inside an existing partition.
            raise HTTPException(400, f"no partition for captured_at {sig.captured_at}: {e}") from e
    return SignalInsertResult(inserted=inserted, content_hash=h)


@app.get("/signals", response_model=list[Signal], tags=["signals"])
def list_signals(
    frm: datetime = Query(..., alias="from", description="start of window (inclusive)"),
    to: datetime = Query(..., description="end of window (exclusive)"),
    influencer_id: int | None = None,
    limit: int = Query(100, le=1000),
):
    """A time window is REQUIRED so the query always carries the partition key and prunes.
    Filtering by influencer alone (no window) would fan out across every partition, so the
    API simply doesn't offer that shape."""
    clauses = ["captured_at >= %s", "captured_at < %s"]
    params: list = [frm, to]
    if influencer_id is not None:
        clauses.append("influencer_id = %s")
        params.append(influencer_id)
    params.append(limit)
    sql = (
        "SELECT id, influencer_id, source_id, captured_at, content_hash, payload "
        "FROM raw_signals WHERE " + " AND ".join(clauses) + " "
        "ORDER BY captured_at DESC LIMIT %s"
    )
    with pool.connection() as conn:
        return conn.cursor(row_factory=dict_row).execute(sql, params).fetchall()


# --- rollup (materialized view read path) --------------------------------------


@app.get("/rollup", response_model=list[DailyRollup], tags=["rollup"])
def rollup(influencer_id: int | None = None):
    """Reads the precomputed daily_signal_rollup matview, not raw_signals. The dashboard
    never pays for the aggregate. Refresh happens out of band (make seed / a scheduled job)."""
    sql = "SELECT influencer_id, day, signal_count, source_count FROM daily_signal_rollup"
    params: list = []
    if influencer_id is not None:
        sql += " WHERE influencer_id = %s"
        params.append(influencer_id)
    sql += " ORDER BY day DESC"
    with pool.connection() as conn:
        return conn.cursor(row_factory=dict_row).execute(sql, params).fetchall()


# --- runs (Module 2: fan-out jobs + live SSE progress) -------------------------

# One send-only Celery client for the process, not one per request: apps pool their broker
# connections, so hoisting it means POST /runs reuses a connection instead of dialing Redis
# every time. Created at import, after common.db has run load_local_env, so it sees .env.
_broker = send_only_celery()

_RUN_COLS = "id, status, mode, model, total, done_count, inserted, rated_count, error, created_at, started_at, finished_at"


def _read_run(run_id: int) -> dict | None:
    """One indexed PK lookup for a run's authoritative state. Used for the GET snapshot and,
    via asyncio.to_thread, for the SSE stream's on-connect snapshot."""
    with pool.connection() as conn:
        return conn.cursor(row_factory=dict_row).execute(f"SELECT {_RUN_COLS} FROM runs WHERE id = %s", (run_id,)).fetchone()


@app.post("/runs", response_model=RunCreated, tags=["runs"])
def create_run(trigger: RunTrigger):
    """Kick off a fan-out scrape. The API's half of the split: write the durable record (the
    runs row), then hand off by task NAME over the broker. The worker's dispatch task builds
    the per-influencer chord; this process never imports worker code (see task_contract).
    Returns immediately (202-style): the work happens in the worker, watch it on the stream.
    If the worker is down the row simply sits in 'queued' until one picks it up.

    An unknown provider or a missing provider key is rejected HERE with a 400, before any
    task is enqueued. Fail at the door: a bad model string should never make it into the
    queue where it would surface as N retry-looping worker tasks instead of one clear error."""
    if trigger.model is not None:
        try:
            resolve_model(trigger.model)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    with pool.connection() as conn:
        total = conn.execute("SELECT count(*) FROM influencers").fetchone()[0]
        run_id = conn.execute(
            "INSERT INTO runs (status, mode, total, model) VALUES ('queued', %s, %s, %s) RETURNING id",
            (trigger.mode, total, trigger.model),
        ).fetchone()[0]
    _broker.send_task(
        DISPATCH_RUN,
        kwargs={"run_id": run_id, "mode": trigger.mode, "limit": trigger.limit, "model": trigger.model},
    )
    return RunCreated(run_id=run_id, total=total, mode=trigger.mode, model=trigger.model)


@app.get("/runs", response_model=list[Run], tags=["runs"])
def list_runs(limit: int = Query(20, le=100)):
    """Recent runs, newest first. The dashboard's run history."""
    with pool.connection() as conn:
        return (
            conn.cursor(row_factory=dict_row)
            .execute(f"SELECT {_RUN_COLS} FROM runs ORDER BY created_at DESC LIMIT %s", (limit,))
            .fetchall()
        )


@app.get("/runs/{run_id}", response_model=Run, tags=["runs"])
def get_run(run_id: int):
    """A run's current state, straight from Postgres (the durable record). This is the
    snapshot a client can read at any time, no stream needed."""
    row = _read_run(run_id)
    if row is None:
        raise HTTPException(404, f"run {run_id} not found")
    return row


@app.get("/runs/{run_id}/stream", tags=["runs"])
async def stream_run(run_id: int):
    """Server-Sent Events: live progress for one run, across BOTH phases.

    The run lifecycle is queued -> running -> (rating) -> completed/failed, and the stream
    surfaces it as four event types:
      - `progress`: one per influencer during the scrape phase.
      - `scrape_done`: the scrape chord fanned in and the rollup refreshed. If a model is in
        play the run is now in `rating` (NOT finished), and this is the cue to switch the UI
        from "scraping X/N" to "rating Y/M".
      - `rating`: one per signal rated, carrying rated/rate_total. Decoupled from the scrape
        chord by design (a slow model never blocks the fan-in), so these arrive after
        `scrape_done`.
      - `done`: the run reached a TERMINAL state. The worker owns this decision, it emits
        `done` at fan-in when there's no rating work, or when the last rating lands. The
        stream's only job is to relay deltas and close on `done`.

    The refresh-proof pattern, in order:
      1. subscribe to the Redis channel FIRST, so no delta published during step 2 is lost.
      2. read the runs row from Postgres and send it as the `snapshot` event. A client that
         connects late, or reconnects after a page refresh, gets the true current state here.
      3. if the run is already terminal on connect, send `done` and close.
      4. otherwise relay deltas and close when a `done` arrives.

    A refresh just reopens this endpoint. There's no per-user registration to lose: the run_id
    in the URL is the whole subscription, and the snapshot re-establishes state every time.
    (Reconnecting mid-rating-drain is safe: we subscribe before reading the snapshot, so the
    `done` that ends the rating phase either is already reflected in the snapshot as a terminal
    status, or arrives on the channel after we're listening.)
    """
    channel = f"run:{run_id}"
    _EVENTS = {"progress", "scrape_done", "rating", "done"}

    async def gen():
        r = aioredis.from_url(REDIS_URL)
        pubsub = r.pubsub()
        await pubsub.subscribe(channel)  # (1) subscribe before snapshot: no gap
        try:
            snapshot = await asyncio.to_thread(_read_run, run_id)  # (2) durable state
            if snapshot is None:
                yield {"event": "error", "data": json.dumps({"error": f"run {run_id} not found"})}
                return
            snap_json = Run(**snapshot).model_dump_json()
            yield {"event": "snapshot", "data": snap_json}
            if snapshot["status"] in ("completed", "failed"):
                yield {"event": "done", "data": snap_json}  # (3) already terminal
                return
            # (4) live deltas. The worker decides when the run is done and publishes `done`
            # exactly then (fan-in for a rating-less run, or the final rating). Relay each
            # delta under its own event name and close on `done`.
            async for msg in pubsub.listen():
                if msg["type"] != "message":
                    continue
                text = msg["data"].decode() if isinstance(msg["data"], bytes) else msg["data"]
                mtype = json.loads(text).get("type")
                yield {"event": mtype if mtype in _EVENTS else "progress", "data": text}
                if mtype == "done":
                    return
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
            await r.aclose()

    return EventSourceResponse(gen())


# --- ratings (Module 4: the AI rating layer's read surface) ---------------------


@app.get("/ratings", response_model=list[Rating], tags=["ratings"])
def list_ratings(limit: int = Query(50, le=200), min_relevance: float | None = Query(None, ge=0, le=1)):
    """Recent ratings, newest first. min_relevance filters to what the model considered
    on-thesis, which is the view the Module 5 digest agent will eventually read."""
    sql = "SELECT content_hash, model, relevance, confidence, topics, summary, rated_at FROM signal_ratings"
    params: list = []
    if min_relevance is not None:
        sql += " WHERE relevance >= %s"
        params.append(min_relevance)
    sql += " ORDER BY rated_at DESC LIMIT %s"
    params.append(limit)
    with pool.connection() as conn:
        return conn.cursor(row_factory=dict_row).execute(sql, params).fetchall()


# --- search (Module 6: hybrid lexical + semantic retrieval) ----------------------


@app.get("/search", response_model=SearchResponse, tags=["search"])
def search(
    q: str = Query(..., min_length=1, description="free-text query (words, quoted phrases, -negation)"),
    limit: int = Query(20, ge=1, le=100),
):
    """Hybrid search over signal captions: Postgres full-text (lexical) + pgvector (semantic),
    fused with Reciprocal Rank Fusion. Each hit reports its fused `score` and which halves found
    it (`sources`).

    The semantic half runs only when EMBEDDING_MODEL is set: the query is embedded with the same
    model the documents were, then matched by cosine distance. With no model, or if embedding the
    query fails, `semantic` is false and results are lexical-only (still useful, exact matches
    rank fine). This is the same inert-until-keyed contract the rating layer uses, made visible
    in the response instead of hidden."""
    # embed_query returns None when EMBEDDING_MODEL is unset OR the embed call failed, so the
    # endpoint degrades to lexical-only in both cases rather than 500-ing on a provider hiccup.
    # Shared with the MCP tool (common.search) so both make the identical fallback decision.
    query_embedding = embed_query(q)
    with pool.connection() as conn:
        hits = hybrid_search(conn, q, query_embedding, limit=limit)
    return SearchResponse(query=q, semantic=query_embedding is not None, hits=[SearchHit(**h) for h in hits])


# --- Module 5: agent-written digests ----------------------------------------------

_DIGEST_COLS = "id, status, session_id, content_md, word_count, error, created_at, completed_at"


def _read_digest(digest_id: int) -> dict | None:
    """One PK lookup for a digest's authoritative state, shared by the GET snapshot and
    (via asyncio.to_thread) the SSE stream's on-connect snapshot. Same split as _read_run."""
    with pool.connection() as conn:
        return (
            conn.cursor(row_factory=dict_row)
            .execute(f"SELECT {_DIGEST_COLS} FROM digests WHERE id = %s", (digest_id,))
            .fetchone()
        )


# Module 5: POST /digests no longer TRIGGERS a session. A digest run is started by triggering a
# Managed Agents *deployment* (packages/agents/deployment.yaml); the API is out of that loop, and so is the
# Celery worker that used to babysit the session. What POST keeps is one narrow job: mint a
# pending row and hand back its id. That matters because a manual deployment run replays fixed
# initial_events (no per-run arguments), so nothing outside the agent can inject a fresh digest
# id into the kickoff. The agent creates its own row here first, then delivers into it via
# PUT /digests/{id}/content. Trigger moved out; the row factory stayed, because the delivery
# door still needs a row to write to.


@app.post("/digests", response_model=Digest, tags=["digests"])
def create_digest() -> dict:
    """Mint a queued digest row and return it. Not a trigger, a row factory: the digest agent
    (started by a deployment, packages/agents/deployment.yaml) calls this as its first step to get an id,
    writes the digest, then completes the row via PUT /digests/{id}/content."""
    with pool.connection() as conn:
        row = (
            conn.cursor(row_factory=dict_row)
            .execute("INSERT INTO digests (status) VALUES ('queued') RETURNING " + _DIGEST_COLS)
            .fetchone()
        )
        conn.commit()
    return row


@app.get("/digests", response_model=list[Digest], tags=["digests"])
def list_digests(limit: int = Query(20, le=100)):
    """Recent digests, newest first."""
    with pool.connection() as conn:
        return (
            conn.cursor(row_factory=dict_row)
            .execute(f"SELECT {_DIGEST_COLS} FROM digests ORDER BY created_at DESC LIMIT %s", (limit,))
            .fetchall()
        )


@app.get("/digests/{digest_id}", response_model=Digest, tags=["digests"])
def get_digest(digest_id: int):
    """One digest's current state, content included once the agent has delivered."""
    row = _read_digest(digest_id)
    if row is None:
        raise HTTPException(404, f"digest {digest_id} not found")
    return row


@app.get("/digests/{digest_id}/stream", tags=["digests"])
async def stream_digest(digest_id: int):
    """Server-Sent Events: live narration of one digest agent session.

    This is a RELAY, two streams glued back to back. Upstream, the worker holds the
    Anthropic session event stream and republishes the interesting events as deltas on
    Redis channel digest:{id}. Downstream, this endpoint turns those deltas into SSE.
    The UI couples to our API only; which vendor runs the agent is the worker's secret.

    Event types, in the order a healthy run emits them:
      - `snapshot`: the digests row on connect (always first; refresh-proof state).
      - `status`: the worker created the session, carries session_id for the trace link.
      - `agent_message` / `tool` / `custom_tool` / `custom_tool_result`: narration,
        "what is the agent doing right now", for a ticker UI.
      - `done`: terminal verdict (completed/failed), decided by the WORKER after its
        final row check, because completion is judged by the database (the agent's PUT),
        never by how the transcript looked. Stream closes on it.

    Same refresh-proof choreography as /runs/{run_id}/stream: subscribe FIRST, then
    snapshot from Postgres, then relay; a client connecting after the run ended gets
    snapshot + done and closes without ever touching Redis history (there is none,
    pub/sub is fire-and-forget; the row is the durable record)."""
    channel = f"digest:{digest_id}"
    _EVENTS = {"status", "agent_message", "tool", "custom_tool", "custom_tool_result", "done"}

    async def gen():
        r = aioredis.from_url(REDIS_URL)
        pubsub = r.pubsub()
        await pubsub.subscribe(channel)  # subscribe before snapshot: no gap
        try:
            snapshot = await asyncio.to_thread(_read_digest, digest_id)
            if snapshot is None:
                yield {"event": "error", "data": json.dumps({"error": f"digest {digest_id} not found"})}
                return
            snap_json = Digest(**snapshot).model_dump_json()
            yield {"event": "snapshot", "data": snap_json}
            if snapshot["status"] in ("completed", "failed"):
                yield {"event": "done", "data": snap_json}  # already terminal
                return
            async for msg in pubsub.listen():
                if msg["type"] != "message":
                    continue
                text = msg["data"].decode() if isinstance(msg["data"], bytes) else msg["data"]
                mtype = json.loads(text).get("type")
                yield {"event": mtype if mtype in _EVENTS else "status", "data": text}
                if mtype == "done":
                    return
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
            await r.aclose()

    return EventSourceResponse(gen())


@app.put("/digests/{digest_id}/content", response_model=Digest, tags=["digests"])
def deliver_digest(digest_id: int, body: DigestContent):
    """The agent's delivery door. The digest agent calls this from its sandbox with the
    finished markdown, authenticated by the same vaulted X-API-Key as every data route.
    Storing the content IS what completes the run; the worker watching the session only
    marks failure if the stream ends and this call never arrived.

    PUT because delivery is idempotent by design: re-delivering the same digest (an
    agent retrying a flaky request) just overwrites the row with the same content, no
    duplicate rows, no counter to corrupt."""
    with pool.connection() as conn:
        row = (
            conn.cursor(row_factory=dict_row)
            .execute(
                """
                UPDATE digests SET
                  content_md = %s,
                  word_count = %s,
                  status = 'completed',
                  completed_at = now(),
                  error = NULL
                WHERE id = %s
                RETURNING """
                + _DIGEST_COLS,
                (body.content_md, len(body.content_md.split()), digest_id),
            )
            .fetchone()
        )
        conn.commit()
    if row is None:
        raise HTTPException(404, f"digest {digest_id} not found")
    return row
