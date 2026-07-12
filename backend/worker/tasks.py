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
from pathlib import Path

import psycopg
import redis
from psycopg.rows import dict_row

from common.db import DATABASE_URL
from common.digests import get_rated_signals
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
    Refresh the rollup so it reflects this run, then move the run OUT of the scrape phase.

    This does NOT mean the run is finished. If a model is in play, the run moves to `rating`
    (not `completed`) and stays there while its rate_signal jobs drain, reaching `completed`
    only when the last rating lands (see _announce_rating). A run with no rating work goes
    straight to `completed` here. Either way the scrape work and the rollup refresh are done,
    which is what this barrier is actually responsible for; ratings are decoupled by design so
    a slow model never gates the fan-in."""
    errors = [r for r in results if r and r.get("error")]
    failed = bool(errors and len(errors) == len(results))
    err_text = "; ".join(f"{r['handle']}: {r['error']}" for r in errors) or None

    # REFRESH ... CONCURRENTLY can't run inside a transaction block, so use autocommit.
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        try:
            refresh_rollup(conn, concurrently=True)
        except Exception as e:  # noqa: BLE001
            failed = True
            err_text = f"{(err_text + '; ') if err_text else ''}refresh: {type(e).__name__}: {e}"

        # Decide the post-scrape status atomically, off the row's CURRENT rated_count, so a
        # rate_signal that bumped the counter while this callback was running can't strand the
        # run. Rating is enabled when the run carries a model OR RATING_MODEL is set (has_env).
        #   failed                             -> failed    (terminal)
        #   enabled AND inserted>0 AND rated<inserted -> rating   (still draining, NOT terminal)
        #   else                               -> completed (no ratings, or they all already landed)
        # finished_at is stamped only on a terminal status; `rating` leaves it NULL until the
        # final rating flips the run to completed.
        has_env = default_model() is not None
        final = conn.cursor(row_factory=dict_row).execute(
            """
            UPDATE runs SET
              status = CASE
                WHEN %(failed)s THEN 'failed'
                WHEN (model IS NOT NULL OR %(has_env)s) AND inserted > 0 AND rated_count < inserted
                     THEN 'rating'
                ELSE 'completed'
              END,
              finished_at = CASE
                WHEN %(failed)s THEN now()
                WHEN (model IS NOT NULL OR %(has_env)s) AND inserted > 0 AND rated_count < inserted
                     THEN finished_at
                ELSE now()
              END,
              error = %(err)s
            WHERE id = %(id)s
            RETURNING done_count, total, inserted, rated_count, status, model
            """,
            {"failed": failed, "has_env": has_env, "err": err_text, "id": run_id},
        ).fetchone()

    # rate_total is the rating denominator: one rate_signal was enqueued per inserted signal,
    # but only when a model is in play. `scrape_done` marks the end of the scrape phase without
    # ending the run (status == 'rating'); a terminal status publishes `done` and closes the
    # stream. Some ratings may already be in (rated) by the time the chord fans in.
    rate_total = final["inserted"] if (final["model"] or has_env) else 0
    terminal = final["status"] in ("completed", "failed")
    _publish(run_id, {
        "type": "done" if terminal else "scrape_done",
        "run_id": run_id,
        "status": final["status"],
        "error": err_text,
        "done": final["done_count"],
        "total": final["total"],
        "run_inserted": final["inserted"],
        "rated": final["rated_count"],
        "rate_total": rate_total,
    })
    return {"run_id": run_id, "status": final["status"], **final}


# --- Module 4: the AI rating stage ----------------------------------------------

@celery_app.task(
    name="worker.tasks.rate_signal",
    bind=True,
    max_retries=3,
)
def rate_signal(
    self, content_hash: str, handle: str, caption: str | None, model: str, run_id: int | None = None
) -> str:
    """Rate one signal's content. Idempotent at both ends: skip if a rating for this hash
    already exists (dedup on the INPUT hash, the model's output is non-deterministic), and
    the insert is ON CONFLICT DO NOTHING for the race where the beat sweep and a scrape
    enqueue the same hash. Model failures raise RatingError; we retry with exponential backoff
    up to max_retries, then GIVE UP but still count the signal (see below).

    The model call happens with NO database connection open. It can take minutes on CPU
    inference, and a connection held across it is a pool slot doing nothing.

    run_id ties this rating back to the run that enqueued it (scrape path). EVERY terminal
    outcome (freshly rated, already-rated, lost-race, or gave-up-after-retries) counts once
    toward that run's rated_count via _announce_rating. That total-coverage guarantee is what
    lets the run converge from `rating` to `completed`: rated_count reaches inserted no matter
    how individual ratings land. sweep-originated ratings pass run_id=None and stay silent
    (they aren't part of any run's denominator).

    Manual retry (not autoretry_for) so the give-up path runs our own code, announcing the
    signal instead of letting the task die silently and stranding the run in `rating`."""
    with psycopg.connect(DATABASE_URL) as conn:
        if conn.execute(
            "SELECT 1 FROM signal_ratings WHERE content_hash = %s", (content_hash,)
        ).fetchone():
            _announce_rating(run_id, handle, content_hash)
            return "already-rated"

    try:
        rating = rate_caption(handle, caption or "", model)
    except RatingError as exc:
        if self.request.retries >= self.max_retries:
            # Out of retries. Count the signal so the run's rated_count still reaches inserted
            # and the run can complete; the beat sweep re-rates it later (run_id=None there, so
            # no double count) once a model is healthy.
            _announce_rating(run_id, handle, content_hash)
            return f"gave-up: {type(exc).__name__}"
        raise self.retry(exc=exc, countdown=min(2 ** self.request.retries, 300))

    with psycopg.connect(DATABASE_URL) as conn:
        did = insert_rating(conn, content_hash, model, rating)
        conn.commit()
    _announce_rating(run_id, handle, content_hash)
    return "rated" if did else "lost-race"


def _announce_rating(run_id: int | None, handle: str, content_hash: str) -> None:
    """Bump the run's rated_count and publish a delta. One call per terminally-handled signal
    on the scrape path. No-op when run_id is None (sweep backstop): those ratings aren't part of
    a run's denominator, so counting them would push rated_count past inserted.

    The bump that brings rated_count up to inserted is the run's TRUE terminal moment, so this
    also flips `rating` -> `completed` and stamps finished_at, in the SAME atomic UPDATE (the
    `status = 'rating'` guard means exactly one concurrent finisher wins the flip). That bump
    publishes `done`; every earlier bump publishes a `rating` delta. If the run isn't in
    `rating` yet (finalize_run hasn't run, status still 'running'), the counter just increments
    and finalize picks the right status off it later.

    The counter lives in Postgres (durable snapshot for a reconnecting SSE client), the delta
    goes to Redis (live push for a connected one), the same two-store split scrape progress
    uses. rate_total is echoed as runs.inserted so a subscriber renders N/M off one message."""
    if run_id is None:
        return
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            """
            UPDATE runs SET
              rated_count = rated_count + 1,
              status = CASE WHEN status = 'rating' AND rated_count + 1 >= inserted
                            THEN 'completed' ELSE status END,
              finished_at = CASE WHEN status = 'rating' AND rated_count + 1 >= inserted
                                 THEN now() ELSE finished_at END
            WHERE id = %s
            RETURNING rated_count, inserted, status
            """,
            (run_id,),
        ).fetchone()
        conn.commit()
    if row is None:  # run row deleted out from under us; nothing to announce
        return
    rated, inserted, status = row
    terminal = status == "completed"
    _publish(run_id, {
        "type": "done" if terminal else "rating",
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


# --- Module 5: the digest agent session -------------------------------------------

# The Managed Agents object IDs (agent, environment, vault, memory store) created by
# m5_agents/apply.sh. Committed, not secret; the vault holds the actual secret.
_M5_RESOURCES = Path(__file__).resolve().parent.parent / "m5_agents" / "resources.json"


@celery_app.task(name="worker.tasks.run_digest_session", bind=True)
def run_digest_session(self, digest_id: int, base_url: str) -> str:
    """Babysit one Managed Agent session until it delivers a digest (or fails to).

    This is m5_agents/run_digest.py relocated from the laptop into the worker, which is
    the point: the custom-tool listener is just a process holding the session's event
    stream, and the worker is a fine place for that process to live. Three channels,
    never crossing:

      worker -> Anthropic   sessions.create + kickoff (plain POSTs), then the one-way
                            SSE event stream this task sits on until the session ends.
      agent  -> our API     the sandbox curls BASE_URL with the vaulted X-API-Key for
                            data, and DELIVERS the result via PUT /digests/{id}/content.
                            None of that touches this task.
      agent  -> this task   the one custom tool, get_rated_signals: the stream announces
                            the call, this task runs the query with ITS db credentials
                            and POSTs rows back. Goes away when the tool becomes a plain
                            endpoint.

    Completion is judged by the DATABASE, not the stream: the agent's PUT flips the row
    to `completed`. If the stream ends and the row never flipped, the session finished
    without delivering, and that's a failure no matter how happy the transcript looks.

    This task is also a RELAY: each interesting upstream event is republished as a
    small delta to Redis channel digest:{id}, which GET /digests/{id}/stream turns
    into SSE for a UI. Downstream clients couple to OUR api, never to Anthropic's
    stream; swapping the agent vendor later touches this task and nothing else. The
    worker is the only publisher, and it alone decides when `done` goes out (after
    the final row check), same single-owner rule as the run stream's fan-in."""
    import anthropic

    resources = json.loads(_M5_RESOURCES.read_text())
    client = anthropic.Anthropic()

    session = client.beta.sessions.create(
        agent={
            "type": "agent",
            "id": resources["agent_id"],
            "version": resources["agent_version"],  # pinned: an update mid-run can't change behavior
        },
        environment_id=resources["environment_id"],
        vault_ids=[resources["vault_id"]],
        title=f"digest {digest_id}",
        resources=[
            {
                "type": "memory_store",
                "memory_store_id": resources["memory_store_id"],
                "access": "read_write",
                "instructions": (
                    "Previous weekly digests, one dated file per week. Read the most "
                    "recent one before writing this week's, then save a dated copy of "
                    "the new digest here."
                ),
            }
        ],
    )

    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            "UPDATE digests SET status = 'running', session_id = %s WHERE id = %s",
            (session.id, digest_id),
        )
        conn.commit()

    def relay(message: dict) -> None:
        """Republish one upstream event as a digest:{id} delta. Best-effort: the stream
        is a live view, the row is the record, so a Redis hiccup must never kill the
        session babysitter."""
        try:
            _redis.publish(f"digest:{digest_id}", json.dumps(message))
        except Exception:  # noqa: BLE001
            pass

    relay({"type": "status", "status": "running", "session_id": session.id})

    error: str | None = None
    try:
        # Stream-first: open the SSE stream BEFORE the kickoff so no early event races
        # past an unattached consumer (same subscribe-before-snapshot discipline as our
        # own /runs/{id}/stream endpoint).
        with client.beta.sessions.events.stream(session_id=session.id) as stream:
            client.beta.sessions.events.send(
                session_id=session.id,
                events=[{
                    "type": "user.message",
                    "content": [{
                        "type": "text",
                        "text": (
                            f"Write this week's digest. Digest id: {digest_id}. "
                            f"API base URL: {base_url}"
                        ),
                    }],
                }],
            )

            for event in stream:
                if event.type == "agent.message":
                    # Narration for the UI. Truncated defensively; the full transcript
                    # lives in the Console trace, this is a progress ticker.
                    for block in event.content:
                        if block.type == "text" and block.text.strip():
                            relay({"type": "agent_message", "text": block.text[:2000]})

                elif event.type == "agent.tool_use":
                    # Sandbox-side tools (bash, file writes): name only, inputs can
                    # hold whole file bodies.
                    relay({"type": "tool", "name": event.name})

                elif event.type == "agent.custom_tool_use":
                    # Our one speaking part. Everything else on the stream is narration.
                    relay({"type": "custom_tool", "name": event.name,
                           "input": event.input or {}})
                    try:
                        rows = get_rated_signals(**(event.input or {}))
                        result = json.dumps(rows, default=str)
                    except Exception as e:  # the agent should hear failures, not hang
                        result = f"tool error: {e}"
                    client.beta.sessions.events.send(
                        session_id=session.id,
                        events=[{
                            "type": "user.custom_tool_result",
                            "custom_tool_use_id": event.id,  # the sevt_ event id, not a toolu_ id
                            "content": [{"type": "text", "text": result}],
                        }],
                    )
                    relay({"type": "custom_tool_result", "name": event.name,
                           "bytes": len(result)})

                elif event.type == "session.status_idle":
                    # Idle with requires_action means "waiting on a tool result", keep
                    # listening. Any other stop_reason (end_turn) is the session done.
                    if event.stop_reason.type == "requires_action":
                        continue
                    break

                elif event.type == "session.status_terminated":
                    error = "session terminated before finishing"
                    break
    except Exception as e:  # noqa: BLE001 (mark the row failed rather than losing the run)
        error = f"{type(e).__name__}: {e}"

    # The agent's PUT is the completion signal. No PUT by stream end = failed run.
    with psycopg.connect(DATABASE_URL) as conn:
        status = conn.execute(
            "SELECT status FROM digests WHERE id = %s", (digest_id,)
        ).fetchone()[0]
        if status != "completed":
            error = error or "session ended without delivering a digest (no PUT received)"
            conn.execute(
                "UPDATE digests SET status = 'failed', error = %s, completed_at = now() "
                "WHERE id = %s",
                (error, digest_id),
            )
            conn.commit()
            relay({"type": "done", "status": "failed", "error": error})
            return "failed"
    relay({"type": "done", "status": "completed"})
    return "completed"


def start_digest(base_url: str | None = None) -> dict:
    """Create the digests row and enqueue the session babysitter. Called by POST /digests.
    Row-first, same as start_run: the id exists before any work does, so the kickoff
    message can tell the agent where to deliver."""
    base = base_url or os.environ.get("SYSDESIGN_PUBLIC_URL", "https://sysdesign.thedefrag.ai")
    with psycopg.connect(DATABASE_URL) as conn:
        digest_id = conn.execute(
            "INSERT INTO digests (status) VALUES ('queued') RETURNING id"
        ).fetchone()[0]
        conn.commit()
    run_digest_session.delay(digest_id, base)
    return {"digest_id": digest_id, "base_url": base}


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
