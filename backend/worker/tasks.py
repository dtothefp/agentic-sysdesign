"""The fan-out / fan-in job graph and the Redis progress protocol.

Shape of a run:

    start_run()  ->  chord( group(scrape_influencer x N) , finalize_run )

    fan-out:  one scrape_influencer task per influencer, all queued at once, workers pull
              them in parallel. Each lands its signals, bumps the run's done_count, and
              PUBLISHes a progress delta to Redis channel run:{id}.
    fan-in:   finalize_run is the chord callback. Celery fires it exactly once, after every
              task in the group has finished. It refreshes the materialized view (so the
              rollup reflects this run) and flips the run to completed.

The SSE endpoint reads the runs row for a snapshot on connect, then subscribes to run:{id}
for these deltas. Durable state (runs table) plus live deltas (Redis) is what makes progress
survive a page refresh.
"""
import json
import os
from datetime import datetime, timezone

import psycopg
import redis
from psycopg.rows import dict_row

from common.db import DATABASE_URL
from common.signals import refresh_rollup
from worker.celery_app import celery_app
from worker.scrape import scrape_influencer_demo, scrape_influencer_live

_redis = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379"))


def _channel(run_id: int) -> str:
    return f"run:{run_id}"


def _publish(run_id: int, message: dict) -> None:
    """Fire a progress delta onto the run's channel. Best-effort: pub/sub only reaches
    whoever is subscribed right now, and that's fine because the runs table holds the
    authoritative snapshot a late/reconnecting subscriber reads first."""
    _redis.publish(_channel(run_id), json.dumps(message))


# --- fan-out unit --------------------------------------------------------------

@celery_app.task(name="worker.tasks.scrape_influencer", bind=True)
def scrape_influencer(self, run_id: int, inf: dict, run_ts: str, mode: str, limit: int) -> dict:
    """Scrape one influencer, record progress, publish a delta. Never raises: a single
    influencer failing shouldn't sink the whole run or block the chord callback, so errors
    are captured in the return value and still count toward done_count."""
    error: str | None = None
    inserted = 0
    with psycopg.connect(DATABASE_URL) as conn:
        # first task to touch the run flips it queued -> running and stamps started_at.
        # COALESCE makes it idempotent under the race of N tasks starting together.
        conn.execute(
            "UPDATE runs SET status = CASE WHEN status = 'queued' THEN 'running' ELSE status END, "
            "started_at = COALESCE(started_at, now()) WHERE id = %s",
            (run_id,),
        )
        conn.commit()
        try:
            scrape = scrape_influencer_demo if mode == "demo" else scrape_influencer_live
            inserted = scrape(conn, inf, run_ts, limit)
        except Exception as e:  # noqa: BLE001 (deliberately swallow so the chord still fires)
            error = f"{type(e).__name__}: {e}"
            conn.rollback()

        row = conn.execute(
            "UPDATE runs SET done_count = done_count + 1, inserted = inserted + %s "
            "WHERE id = %s RETURNING done_count, total, inserted",
            (inserted, run_id),
        ).fetchone()
        conn.commit()

    done, total, run_inserted = row
    _publish(run_id, {
        "type": "progress",
        "run_id": run_id,
        "influencer": inf["instagram_handle"],
        "inserted": inserted,
        "error": error,
        "done": done,
        "total": total,
        "run_inserted": run_inserted,
    })
    return {"handle": inf["instagram_handle"], "inserted": inserted, "error": error}


# --- fan-in barrier ------------------------------------------------------------

@celery_app.task(name="worker.tasks.finalize_run", bind=True)
def finalize_run(self, results: list[dict], run_id: int) -> dict:
    """Chord callback: runs once, after every scrape_influencer in the group has returned.
    Refresh the rollup so it reflects this run, then close the run out and publish `done`."""
    errors = [r for r in results if r and r.get("error")]
    status = "failed" if errors and len(errors) == len(results) else "completed"
    err_text = "; ".join(f"{r['handle']}: {r['error']}" for r in errors) or None

    # REFRESH ... CONCURRENTLY can't run inside a transaction block, so use autocommit.
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        try:
            refresh_rollup(conn, concurrently=True)
        except Exception as e:  # noqa: BLE001
            status = "failed"
            err_text = f"{(err_text + '; ') if err_text else ''}refresh: {type(e).__name__}: {e}"

        final = conn.cursor(row_factory=dict_row).execute(
            "UPDATE runs SET status = %s, finished_at = now(), error = %s "
            "WHERE id = %s RETURNING done_count, total, inserted",
            (status, err_text, run_id),
        ).fetchone()

    _publish(run_id, {
        "type": "done",
        "run_id": run_id,
        "status": status,
        "error": err_text,
        "done": final["done_count"],
        "total": final["total"],
        "run_inserted": final["inserted"],
    })
    return {"run_id": run_id, "status": status, **final}


# --- periodic backstop ---------------------------------------------------------

@celery_app.task(name="worker.tasks.refresh_rollup_task", bind=True)
def refresh_rollup_task(self) -> str:
    """Celery-beat backstop. The fan-in already refreshes after each run; this catches signals
    that arrived by any other path, so the dashboard is never more than ~5 min stale."""
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        refresh_rollup(conn, concurrently=True)
    return "refreshed"


# --- trigger (called from the API) ---------------------------------------------

def start_run(mode: str = "live", limit: int = 5) -> dict:
    """Create a runs row and enqueue the fan-out chord. Returns the run_id and how many
    influencers it fanned out to. Called by POST /runs in the API process."""
    run_ts = datetime.now(timezone.utc).isoformat()
    with psycopg.connect(DATABASE_URL) as conn:
        influencers = conn.cursor(row_factory=dict_row).execute(
            "SELECT id, instagram_handle, last_scraped_at FROM influencers ORDER BY id"
        ).fetchall()
        run_id = conn.execute(
            "INSERT INTO runs (status, mode, total) VALUES ('queued', %s, %s) RETURNING id",
            (mode, len(influencers)),
        ).fetchone()[0]
        conn.commit()

    # Celery's JSON serializer can't ship a datetime, so pass the watermark as isoformat/None.
    payload_infs = [
        {
            "id": i["id"],
            "instagram_handle": i["instagram_handle"],
            "last_scraped_at": i["last_scraped_at"].isoformat() if i["last_scraped_at"] else None,
        }
        for i in influencers
    ]

    # chord = a group (the fan-out) plus a callback (the fan-in barrier). Celery tracks the
    # group in the result backend and fires finalize_run once all N have completed.
    from celery import chord

    header = [
        scrape_influencer.s(run_id, inf, run_ts, mode, limit) for inf in payload_infs
    ]
    chord(header)(finalize_run.s(run_id))
    return {"run_id": run_id, "total": len(payload_infs), "mode": mode}
