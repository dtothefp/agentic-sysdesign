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
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security
from fastapi.routing import APIRoute
from fastapi.security.api_key import APIKeyHeader
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from redis import asyncio as aioredis
from sse_starlette.sse import EventSourceResponse

from api.models import (
    DailyRollup,
    Influencer,
    InfluencerIn,
    InfluencerWatermark,
    Rating,
    Run,
    RunCreated,
    RunTrigger,
    Signal,
    SignalIn,
    SignalInsertResult,
    Source,
    SourceIn,
)
from common.db import DATABASE_URL
from common.rating import resolve_model
from common.signals import insert_signal
from worker.tasks import start_run

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
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
            .execute(
                "SELECT id, name, instagram_handle, last_scraped_at, created_at "
                "FROM influencers ORDER BY name"
            )
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
            raise HTTPException(400, f"influencer_id {s.influencer_id} does not exist")


# --- signals -------------------------------------------------------------------

@app.post("/signals", response_model=SignalInsertResult, tags=["signals"])
def create_signal(sig: SignalIn):
    """Idempotent write. content_hash is derived server-side, so identical payloads dedupe no
    matter who sends them. Returns inserted=False when the exact signal already existed. The
    actual upsert is common.signals.insert_signal, the same function the Celery worker calls,
    so there's literally one write path."""
    with pool.connection() as conn:
        try:
            inserted, h = insert_signal(
                conn, sig.influencer_id, sig.captured_at, sig.payload, sig.source_id
            )
        except psycopg.errors.ForeignKeyViolation:
            raise HTTPException(400, f"influencer_id {sig.influencer_id} does not exist")
        except psycopg.errors.CheckViolation as e:
            # no partition covers captured_at. Provision the month (create_month_partition)
            # or pick a captured_at inside an existing partition.
            raise HTTPException(400, f"no partition for captured_at {sig.captured_at}: {e}")
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

_RUN_COLS = (
    "id, status, mode, model, total, done_count, inserted, rated_count, error, "
    "created_at, started_at, finished_at"
)


def _read_run(run_id: int) -> dict | None:
    """One indexed PK lookup for a run's authoritative state. Used for the GET snapshot and,
    via asyncio.to_thread, for the SSE stream's on-connect snapshot."""
    with pool.connection() as conn:
        return (
            conn.cursor(row_factory=dict_row)
            .execute(f"SELECT {_RUN_COLS} FROM runs WHERE id = %s", (run_id,))
            .fetchone()
        )


@app.post("/runs", response_model=RunCreated, tags=["runs"])
def create_run(trigger: RunTrigger):
    """Kick off a fan-out scrape. Creates the runs row, enqueues one Celery task per
    influencer plus a fan-in callback that refreshes the rollup, and returns the run_id.
    Returns immediately (202-style): the work happens in the worker, watch it on the stream.

    An unknown provider or a missing provider key is rejected HERE with a 400, before any
    task is enqueued. Fail at the door: a bad model string should never make it into the
    queue where it would surface as N retry-looping worker tasks instead of one clear error."""
    if trigger.model is not None:
        try:
            resolve_model(trigger.model)
        except ValueError as e:
            raise HTTPException(400, str(e))
    return RunCreated(**start_run(mode=trigger.mode, limit=trigger.limit, model=trigger.model))


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
