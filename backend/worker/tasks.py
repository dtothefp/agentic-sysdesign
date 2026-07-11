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
from common.rating import RatingError, default_model, insert_rating, rate_caption
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
def scrape_influencer(
    self, run_id: int, inf: dict, run_ts: str, mode: str, limit: int, model: str | None = None
) -> dict:
    """Scrape one influencer, record progress, publish a delta. Never raises: a single
    influencer failing shouldn't sink the whole run or block the chord callback, so errors
    are captured in the return value and still count toward done_count."""
    error: str | None = None
    inserted = 0
    new_items: list[dict] = []
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
            inserted, new_items = scrape(conn, inf, run_ts, limit)
        except Exception as e:  # noqa: BLE001 (deliberately swallow so the chord still fires)
            error = f"{type(e).__name__}: {e}"
            conn.rollback()

        row = conn.execute(
            "UPDATE runs SET done_count = done_count + 1, inserted = inserted + %s "
            "WHERE id = %s RETURNING done_count, total, inserted",
            (inserted, run_id),
        ).fetchone()
        conn.commit()

    # Module 4: the rating stage rides the write path as NEW WORK, not a longer task. One
    # rate_signal job per newly inserted row, enqueued after this task's own writes are
    # committed, so a slow model call can never stretch a scrape or delay the chord's fan-in.
    # No model (none on the run, none in RATING_MODEL) means the rating layer is inert.
    rating_model = model or default_model()
    if rating_model:
        for item in new_items:
            # run_id rides along so the rating task can bump this run's rated_count and publish
            # a `rating` delta, making the (decoupled) rating phase visible on the SSE stream.
            rate_signal.delay(
                item["content_hash"], inf["instagram_handle"], item["caption"], rating_model, run_id
            )

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
            "WHERE id = %s RETURNING done_count, total, inserted, rated_count, model",
            (status, err_text, run_id),
        ).fetchone()

    # rate_total is the rating denominator: one rate_signal was enqueued per inserted signal,
    # but only when a model is in play (on the run or in RATING_MODEL). No model means no
    # rating phase, so the target is 0 and the SSE stream can close on `done`. Some of those
    # ratings may already be in (rated_count on the done event) when the scrape chord finishes.
    rate_total = final["inserted"] if (final["model"] or default_model()) else 0
    _publish(run_id, {
        "type": "done",
        "run_id": run_id,
        "status": status,
        "error": err_text,
        "done": final["done_count"],
        "total": final["total"],
        "run_inserted": final["inserted"],
        "rated": final["rated_count"],
        "rate_total": rate_total,
    })
    return {"run_id": run_id, "status": status, **final}


# --- Module 4: the AI rating stage ----------------------------------------------

@celery_app.task(
    name="worker.tasks.rate_signal",
    bind=True,
    autoretry_for=(RatingError,),
    retry_backoff=True,          # 1s, 2s, 4s... between attempts
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
)
def rate_signal(
    self, content_hash: str, handle: str, caption: str | None, model: str, run_id: int | None = None
) -> str:
    """Rate one signal's content. Idempotent at both ends: skip if a rating for this hash
    already exists (dedup on the INPUT hash, the model's output is non-deterministic), and
    the insert is ON CONFLICT DO NOTHING for the race where the beat sweep and a scrape
    enqueue the same hash. Model failures raise RatingError, which Celery retries with
    backoff; after max_retries the signal stays unrated and the sweep picks it up later.

    The model call happens with NO database connection open. It can take minutes on CPU
    inference, and a connection held across it is a pool slot doing nothing.

    run_id ties this rating back to the run that enqueued it (scrape path). Every terminal
    outcome (freshly rated, already-rated, lost-race) counts once toward that run's
    rated_count and publishes a `rating` delta, so the SSE stream shows the rating phase
    draining instead of going silent between `progress` and `done`. sweep-originated ratings
    pass run_id=None and stay silent (they aren't part of any run's denominator)."""
    with psycopg.connect(DATABASE_URL) as conn:
        if conn.execute(
            "SELECT 1 FROM signal_ratings WHERE content_hash = %s", (content_hash,)
        ).fetchone():
            _announce_rating(run_id, handle, content_hash)
            return "already-rated"

    rating = rate_caption(handle, caption or "", model)

    with psycopg.connect(DATABASE_URL) as conn:
        did = insert_rating(conn, content_hash, model, rating)
        conn.commit()
    _announce_rating(run_id, handle, content_hash)
    return "rated" if did else "lost-race"


def _announce_rating(run_id: int | None, handle: str, content_hash: str) -> None:
    """Bump the run's rated_count and publish a `rating` delta. One call per terminally-rated
    signal on the scrape path. No-op when run_id is None (sweep backstop): those ratings aren't
    part of a run's denominator, so counting them would push rated_count past inserted.

    The counter lives in Postgres (durable snapshot for a reconnecting SSE client) and the delta
    goes to Redis (live push for a currently-connected one), the same two-store split the scrape
    progress uses. rate_total is echoed as runs.inserted so a late subscriber can render N/M off
    a single message without reading the row."""
    if run_id is None:
        return
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            "UPDATE runs SET rated_count = rated_count + 1 WHERE id = %s "
            "RETURNING rated_count, inserted, status",
            (run_id,),
        ).fetchone()
        conn.commit()
    if row is None:  # run row deleted out from under us; nothing to announce
        return
    rated, inserted, status = row
    _publish(run_id, {
        "type": "rating",
        "run_id": run_id,
        "influencer": handle,
        "content_hash": content_hash,
        "rated": rated,
        "rate_total": inserted,
        "run_status": status,
    })


# --- periodic backstops ----------------------------------------------------------

@celery_app.task(name="worker.tasks.refresh_rollup_task", bind=True)
def refresh_rollup_task(self) -> str:
    """Celery-beat backstop. The fan-in already refreshes after each run; this catches signals
    that arrived by any other path, so the dashboard is never more than ~5 min stale."""
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        refresh_rollup(conn, concurrently=True)
    return "refreshed"


@celery_app.task(name="worker.tasks.sweep_unrated", bind=True)
def sweep_unrated(self, batch: int = 50) -> str:
    """Celery-beat backstop for ratings, same pattern as the rollup refresh. The scrape path
    already enqueues a rate_signal per new row; this catches whatever slipped through (a
    rating that exhausted its retries, signals inserted via the API). Bounded batch per tick
    so a backlog drains gradually instead of flooding the queue.

    Only real content (source = 'instagram') is swept. The 4000 seeded drill signals and
    demo-run synthetics are excluded on purpose, a backstop that silently enqueues thousands
    of model calls against seed data is a bill, not a safety net. Demo-run signals still get
    rated via the scrape-time enqueue, where it's an explicit choice."""
    model = default_model()
    if not model:
        return "disabled (RATING_MODEL not set)"
    with psycopg.connect(DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (r.content_hash) r.content_hash, i.instagram_handle, "
            "       r.payload->>'caption' AS caption "
            "FROM raw_signals r JOIN influencers i ON i.id = r.influencer_id "
            "WHERE r.payload->>'source' = 'instagram' "
            "  AND NOT EXISTS (SELECT 1 FROM signal_ratings sr WHERE sr.content_hash = r.content_hash) "
            "ORDER BY r.content_hash, r.captured_at DESC LIMIT %s",
            (batch,),
        ).fetchall()
    for content_hash, handle, caption in rows:
        rate_signal.delay(content_hash, handle, caption, model)
    return f"enqueued {len(rows)}"


# --- trigger (called from the API) ---------------------------------------------

def start_run(mode: str = "live", limit: int = 5, model: str | None = None) -> dict:
    """Create a runs row and enqueue the fan-out chord. Returns the run_id and how many
    influencers it fanned out to. Called by POST /runs in the API process.

    model is the data-plane half of the rating design: which model rates this run's new
    signals rides the request and is stored on the row (so two runs can rate with two models
    and be diffed). None falls back to the worker's RATING_MODEL default; if that's unset
    too, the run scrapes without rating."""
    run_ts = datetime.now(timezone.utc).isoformat()
    with psycopg.connect(DATABASE_URL) as conn:
        influencers = conn.cursor(row_factory=dict_row).execute(
            "SELECT id, instagram_handle, last_scraped_at FROM influencers ORDER BY id"
        ).fetchall()
        run_id = conn.execute(
            "INSERT INTO runs (status, mode, total, model) VALUES ('queued', %s, %s, %s) RETURNING id",
            (mode, len(influencers), model),
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
        scrape_influencer.s(run_id, inf, run_ts, mode, limit, model) for inf in payload_infs
    ]
    chord(header)(finalize_run.s(run_id))
    return {"run_id": run_id, "total": len(payload_infs), "mode": mode, "model": model}
