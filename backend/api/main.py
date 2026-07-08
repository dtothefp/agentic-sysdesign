"""FastAPI surface over the Module 1 schema.

Deliberately thin. Two things it's built to demonstrate, because they're the interview
points the schema exists to make:

  1. Every write is the idempotent `INSERT ... ON CONFLICT DO NOTHING` upsert. The API
     computes content_hash server-side (one place, so clients can't disagree), so re-POSTing
     the identical signal is a no-op. That's the at-least-once story.
  2. The signals read REQUIRES a competitor + time window, so it always carries the
     partition key and prunes to the relevant month(s) instead of fanning across all of them.

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
    Competitor,
    CompetitorIn,
    DailyRollup,
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
    {"name": "competitors", "description": "The entities we track."},
    {"name": "sources", "description": "Where a competitor's signals come from."},
    {"name": "signals", "description": "The partitioned raw_signals firehose."},
    {"name": "rollup", "description": "Precomputed daily counts (materialized view)."},
]

app = FastAPI(
    title="sysdesign API",
    version="0.1.0",
    description=(
        "Thin API over the Module 1 competitor-intelligence schema. FastAPI generates the "
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


# --- competitors ---------------------------------------------------------------

@app.get("/competitors", response_model=list[Competitor], tags=["competitors"])
def list_competitors():
    with pool.connection() as conn:
        return (
            conn.cursor(row_factory=dict_row)
            .execute("SELECT id, name, domain, created_at FROM competitors ORDER BY name")
            .fetchall()
        )


@app.post("/competitors", response_model=Competitor, tags=["competitors"])
def create_competitor(c: CompetitorIn):
    # idempotent on the unique name: re-creating a competitor just returns the existing row
    # (and fills in a domain if one wasn't set before).
    with pool.connection() as conn:
        return (
            conn.cursor(row_factory=dict_row)
            .execute(
                "INSERT INTO competitors (name, domain) VALUES (%s, %s) "
                "ON CONFLICT (name) DO UPDATE "
                "SET domain = COALESCE(EXCLUDED.domain, competitors.domain) "
                "RETURNING id, name, domain, created_at",
                (c.name, c.domain),
            )
            .fetchone()
        )


# --- sources -------------------------------------------------------------------

@app.get("/sources", response_model=list[Source], tags=["sources"])
def list_sources(competitor_id: int | None = None):
    sql = "SELECT id, competitor_id, kind, url, created_at FROM sources"
    params: list = []
    if competitor_id is not None:
        sql += " WHERE competitor_id = %s"
        params.append(competitor_id)
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
                    "INSERT INTO sources (competitor_id, kind, url) VALUES (%s, %s, %s) "
                    "RETURNING id, competitor_id, kind, url, created_at",
                    (s.competitor_id, s.kind, s.url),
                )
                .fetchone()
            )
        except psycopg.errors.ForeignKeyViolation:
            raise HTTPException(400, f"competitor_id {s.competitor_id} does not exist")


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
                "(competitor_id, source_id, captured_at, content_hash, payload) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (competitor_id, content_hash, captured_at) DO NOTHING",
                (sig.competitor_id, sig.source_id, sig.captured_at, h, Jsonb(sig.payload)),
            )
        except psycopg.errors.ForeignKeyViolation:
            raise HTTPException(400, f"competitor_id {sig.competitor_id} does not exist")
        except psycopg.errors.CheckViolation as e:
            # no partition covers captured_at. Provision the month (create_month_partition)
            # or pick a captured_at inside an existing partition.
            raise HTTPException(400, f"no partition for captured_at {sig.captured_at}: {e}")
    return SignalInsertResult(inserted=cur.rowcount == 1, content_hash=h)


@app.get("/signals", response_model=list[Signal], tags=["signals"])
def list_signals(
    frm: datetime = Query(..., alias="from", description="start of window (inclusive)"),
    to: datetime = Query(..., description="end of window (exclusive)"),
    competitor_id: int | None = None,
    limit: int = Query(100, le=1000),
):
    """A time window is REQUIRED so the query always carries the partition key and prunes.
    Filtering by competitor alone (no window) would fan out across every partition, so the
    API simply doesn't offer that shape."""
    clauses = ["captured_at >= %s", "captured_at < %s"]
    params: list = [frm, to]
    if competitor_id is not None:
        clauses.append("competitor_id = %s")
        params.append(competitor_id)
    params.append(limit)
    sql = (
        "SELECT id, competitor_id, source_id, captured_at, content_hash, payload "
        "FROM raw_signals WHERE " + " AND ".join(clauses) + " "
        "ORDER BY captured_at DESC LIMIT %s"
    )
    with pool.connection() as conn:
        return conn.cursor(row_factory=dict_row).execute(sql, params).fetchall()


# --- rollup (materialized view read path) --------------------------------------

@app.get("/rollup", response_model=list[DailyRollup], tags=["rollup"])
def rollup(competitor_id: int | None = None):
    """Reads the precomputed daily_signal_rollup matview, not raw_signals. The dashboard
    never pays for the aggregate. Refresh happens out of band (make seed / a scheduled job)."""
    sql = "SELECT competitor_id, day, signal_count, source_count FROM daily_signal_rollup"
    params: list = []
    if competitor_id is not None:
        sql += " WHERE competitor_id = %s"
        params.append(competitor_id)
    sql += " ORDER BY day DESC"
    with pool.connection() as conn:
        return conn.cursor(row_factory=dict_row).execute(sql, params).fetchall()
