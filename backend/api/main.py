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
from contextlib import asynccontextmanager
from datetime import datetime

import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.routing import APIRoute
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from api.models import (
    DailyRollup,
    Influencer,
    InfluencerIn,
    InfluencerWatermark,
    Signal,
    SignalIn,
    SignalInsertResult,
    Source,
    SourceIn,
)
from common.db import DATABASE_URL
from common.hashing import content_hash

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open()
    yield
    pool.close()


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
    """Idempotent write. content_hash is derived here, so identical payloads dedupe no
    matter who sends them. Returns inserted=False when the exact signal already existed."""
    h = content_hash(sig.payload)
    with pool.connection() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO raw_signals "
                "(influencer_id, source_id, captured_at, content_hash, payload) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (influencer_id, content_hash, captured_at) DO NOTHING",
                (sig.influencer_id, sig.source_id, sig.captured_at, h, Jsonb(sig.payload)),
            )
        except psycopg.errors.ForeignKeyViolation:
            raise HTTPException(400, f"influencer_id {sig.influencer_id} does not exist")
        except psycopg.errors.CheckViolation as e:
            # no partition covers captured_at. Provision the month (create_month_partition)
            # or pick a captured_at inside an existing partition.
            raise HTTPException(400, f"no partition for captured_at {sig.captured_at}: {e}")
    return SignalInsertResult(inserted=cur.rowcount == 1, content_hash=h)


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
