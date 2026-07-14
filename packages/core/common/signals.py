"""The one signal write path, shared by the API handler and the Celery worker.

Module 1 kept every write behind the HTTP API so there was a single idempotent code path.
Module 2 adds a second writer (the background worker), and the honest way to keep "one write
path" true is to make the upsert a function both callers import, rather than duplicating the
`ON CONFLICT` SQL. content_hash is still derived here (from common.hashing), so a payload can
never disagree with itself no matter who inserts it.

refresh_rollup lives here too because it's the write-side counterpart: after signals land,
the materialized view has to be recomputed, and the fan-in step of a run calls it.
"""

from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from common.hashing import content_hash


def insert_signal(
    conn: psycopg.Connection,
    influencer_id: int,
    captured_at: datetime,
    payload: dict[str, Any],
    source_id: int | None = None,
) -> tuple[bool, str]:
    """Idempotent upsert of one signal. Returns (inserted, content_hash).

    inserted is False when the exact signal already existed (the ON CONFLICT dedup fired),
    which is the at-least-once story: reprocessing the same post twice is a no-op. Raises
    psycopg errors (ForeignKeyViolation for an unknown influencer, CheckViolation when no
    partition covers captured_at) for the caller to translate into its own error shape.
    """
    h = content_hash(payload)
    cur = conn.execute(
        "INSERT INTO raw_signals "
        "(influencer_id, source_id, captured_at, content_hash, payload) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (influencer_id, content_hash, captured_at) DO NOTHING",
        (influencer_id, source_id, captured_at, h, Jsonb(payload)),
    )
    return cur.rowcount == 1, h


def refresh_rollup(conn: psycopg.Connection, concurrently: bool = True) -> None:
    """Recompute the daily_signal_rollup materialized view.

    CONCURRENTLY rebuilds it without taking an ACCESS EXCLUSIVE lock, so dashboard readers
    keep hitting the old copy until the new one is ready and swapped in. That only works
    because Module 1 gave the matview a UNIQUE index on (influencer_id, day); without it
    Postgres rejects a concurrent refresh. CONCURRENTLY also can't run inside a transaction
    block, so the caller must be in autocommit.
    """
    kw = "CONCURRENTLY " if concurrently else ""
    conn.execute(f"REFRESH MATERIALIZED VIEW {kw}daily_signal_rollup")
