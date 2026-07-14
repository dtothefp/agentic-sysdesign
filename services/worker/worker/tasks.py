"""The fan-out / fan-in job graph and the Redis progress protocol.

Shape of a run:

    dispatch_run (sent by the API as DISPATCH_RUN)  ->  chord( group(scrape_influencer x N) , finalize_run )

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
from datetime import UTC, datetime

import psycopg
import redis
from common.db import DATABASE_URL
from common.embedding import EmbeddingError, default_embedding_model, embed_text, insert_embedding
from common.rating import RatingError, default_model, insert_rating, rate_caption
from common.signals import refresh_rollup
from psycopg.rows import dict_row
from task_contract import DISPATCH_RUN

from worker.celery_app import celery_app
from worker.scrape import scrape_influencer_demo, scrape_influencer_live

_redis = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379"))

# Module 4 semantic cache: max cosine DISTANCE at which a prior rating is copied instead of
# calling the model. 0.05 distance == 0.95 cosine similarity, i.e. near-identical captions
# (a reposted quote, a creator's boilerplate CTA). Conservative on purpose: a loose threshold
# would copy a rating across posts that merely share a topic, which is wrong, the cache is for
# DUPLICATE-ish content, not similar content. Tunable via env without a code change.
_CACHE_MAX_DISTANCE = float(os.environ.get("RATING_CACHE_MAX_DISTANCE", "0.05"))


def _channel(run_id: int) -> str:
    return f"run:{run_id}"


def _publish(run_id: int, message: dict) -> None:
    """Fire a progress delta onto the run's channel. Best-effort: pub/sub only reaches
    whoever is subscribed right now, and that's fine because the runs table holds the
    authoritative snapshot a late/reconnecting subscriber reads first."""
    _redis.publish(_channel(run_id), json.dumps(message))


# --- fan-out unit --------------------------------------------------------------


@celery_app.task(name="worker.tasks.scrape_influencer", bind=True)
def scrape_influencer(self, run_id: int, inf: dict, run_ts: str, mode: str, limit: int, model: str | None = None) -> dict:
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
            rate_signal.delay(item["content_hash"], inf["instagram_handle"], item["caption"], rating_model, run_id)

    # Module 6: the embedding stage is its OWN pipeline, decoupled from rating exactly as rating
    # is decoupled from scraping. Both write signal_embeddings idempotently, so it doesn't matter
    # which fires first, whoever gets there stores the vector and the other's embed call fast-skips
    # on "already-embedded". Inert when EMBEDDING_MODEL is unset (search stays lexical-only).
    if default_embedding_model():
        for item in new_items:
            embed_signal.delay(item["content_hash"], item["caption"])

    done, total, run_inserted = row
    _publish(
        run_id,
        {
            "type": "progress",
            "run_id": run_id,
            "influencer": inf["instagram_handle"],
            "inserted": inserted,
            "error": error,
            "done": done,
            "total": total,
            "run_inserted": run_inserted,
        },
    )
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
        final = (
            conn.cursor(row_factory=dict_row)
            .execute(
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
            )
            .fetchone()
        )

    # rate_total is the rating denominator: one rate_signal was enqueued per inserted signal,
    # but only when a model is in play. `scrape_done` marks the end of the scrape phase without
    # ending the run (status == 'rating'); a terminal status publishes `done` and closes the
    # stream. Some ratings may already be in (rated) by the time the chord fans in.
    rate_total = final["inserted"] if (final["model"] or has_env) else 0
    terminal = final["status"] in ("completed", "failed")
    _publish(
        run_id,
        {
            "type": "done" if terminal else "scrape_done",
            "run_id": run_id,
            "status": final["status"],
            "error": err_text,
            "done": final["done_count"],
            "total": final["total"],
            "run_inserted": final["inserted"],
            "rated": final["rated_count"],
            "rate_total": rate_total,
        },
    )
    return {"run_id": run_id, "status": final["status"], **final}


# --- Module 4: the AI rating stage ----------------------------------------------


@celery_app.task(
    name="worker.tasks.rate_signal",
    bind=True,
    max_retries=3,
)
def rate_signal(self, content_hash: str, handle: str, caption: str | None, model: str, run_id: int | None = None) -> str:
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
        if conn.execute("SELECT 1 FROM signal_ratings WHERE content_hash = %s", (content_hash,)).fetchone():
            _announce_rating(run_id, handle, content_hash)
            return "already-rated"

    # Module 4 semantic cache. If a near-identical caption was already rated, copy that rating and
    # skip the model call entirely. The embedding needed to find the neighbor is ~100x cheaper than
    # a chat completion and doubles as the search vector, so this is a strict win when it hits and
    # a cheap miss when it doesn't. Inert (returns None immediately) when EMBEDDING_MODEL is unset,
    # so the rating layer behaves exactly as before whenever the semantic layer is off.
    cached = _semantic_cache_lookup(content_hash, caption)
    if cached is not None:
        with psycopg.connect(DATABASE_URL) as conn:
            # Tag the model as cache:<model> so a hit is visible in signal_ratings (query
            # WHERE model LIKE 'cache:%' for the hit rate). The rating VALUES are the neighbor's.
            insert_rating(conn, content_hash, f"cache:{model}", cached)
            conn.commit()
        _announce_rating(run_id, handle, content_hash)
        return "cached"

    try:
        rating = rate_caption(handle, caption or "", model)
    except RatingError as exc:
        if self.request.retries >= self.max_retries:
            # Out of retries. Count the signal so the run's rated_count still reaches inserted
            # and the run can complete; the beat sweep re-rates it later (run_id=None there, so
            # no double count) once a model is healthy.
            _announce_rating(run_id, handle, content_hash)
            return f"gave-up: {type(exc).__name__}"
        raise self.retry(exc=exc, countdown=min(2**self.request.retries, 300)) from exc

    with psycopg.connect(DATABASE_URL) as conn:
        did = insert_rating(conn, content_hash, model, rating)
        conn.commit()
    _announce_rating(run_id, handle, content_hash)
    return "rated" if did else "lost-race"


def _semantic_cache_lookup(content_hash: str, caption: str | None) -> dict | None:
    """Module 4 semantic cache probe. Returns a rating to COPY (from the nearest already-rated
    neighbor) when one is within _CACHE_MAX_DISTANCE, else None (rate normally).

    When EMBEDDING_MODEL is set, this first ensures the signal has an embedding: it reuses a
    stored one if present (the search pipeline may have embedded it already), otherwise computes
    and stores it. That stored vector is then the probe for a nearest-neighbor lookup against
    every OTHER signal that already has a rating. The embedding is stored on a miss too, so the
    signal is immediately searchable and can serve as a future cache donor. One embedding, two
    features (search + cache), which is the whole reason both live in signal_embeddings.

    Returns None (rate normally) whenever embeddings are off or the embed call fails, so a flaky
    embedding provider degrades to the plain model path instead of blocking ratings."""
    model = default_embedding_model()
    if not model:
        return None
    with psycopg.connect(DATABASE_URL) as conn:
        stored = conn.execute("SELECT 1 FROM signal_embeddings WHERE content_hash = %s", (content_hash,)).fetchone()
        if not stored:
            try:
                vector = embed_text(caption or "", model)
            except EmbeddingError:
                return None  # provider hiccup: fall back to a normal model call, no cache
            insert_embedding(conn, content_hash, model, vector)
            conn.commit()
        # Nearest already-rated neighbor, probed by THIS signal's stored embedding. The CROSS JOIN
        # pulls the probe vector once; the <=> ORDER BY ... LIMIT 1 is the HNSW-accelerated KNN.
        # Excluding content_hash itself matters only after the insert above (it now has a row).
        row = conn.execute(
            """
            SELECT r.relevance, r.confidence, r.topics, r.summary,
                   e.embedding <=> me.embedding AS dist
            FROM signal_embeddings e
            JOIN signal_ratings r ON r.content_hash = e.content_hash
            CROSS JOIN (SELECT embedding FROM signal_embeddings WHERE content_hash = %s) me
            WHERE e.content_hash <> %s
            ORDER BY e.embedding <=> me.embedding
            LIMIT 1
            """,
            (content_hash, content_hash),
        ).fetchone()
    if row is None:
        return None  # no other rated signal to copy from yet
    relevance, confidence, topics, summary, dist = row
    if dist is None or dist > _CACHE_MAX_DISTANCE:
        return None  # nothing near enough; a rating for merely-similar content would be wrong
    return {"relevance": relevance, "confidence": confidence, "topics": topics, "summary": summary}


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
    _publish(
        run_id,
        {
            "type": "done" if terminal else "rating",
            "run_id": run_id,
            "influencer": handle,
            "content_hash": content_hash,
            "rated": rated,
            "rate_total": inserted,
            "run_status": status,
        },
    )


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


# --- Module 6: the embedding stage (search + the rating cache share this table) ---


@celery_app.task(name="worker.tasks.embed_signal", bind=True, max_retries=3)
def embed_signal(self, content_hash: str, caption: str | None) -> str:
    """Embed one signal's caption into signal_embeddings for hybrid search. Same idempotency and
    retry shape as rate_signal: skip if already embedded (dedup on the input hash), insert is
    ON CONFLICT DO NOTHING for the sweep/scrape race, retry on EmbeddingError with backoff, then
    give up (the sweep re-embeds later once the provider is healthy).

    The embed call happens with NO db connection open, same reason as the model call, it's a
    network round-trip and a held connection would be a pool slot doing nothing. Inert when
    EMBEDDING_MODEL is unset."""
    model = default_embedding_model()
    if not model:
        return "disabled (EMBEDDING_MODEL not set)"
    with psycopg.connect(DATABASE_URL) as conn:
        if conn.execute("SELECT 1 FROM signal_embeddings WHERE content_hash = %s", (content_hash,)).fetchone():
            return "already-embedded"
    try:
        vector = embed_text(caption or "", model)
    except EmbeddingError as exc:
        if self.request.retries >= self.max_retries:
            return f"gave-up: {type(exc).__name__}"
        raise self.retry(exc=exc, countdown=min(2**self.request.retries, 300)) from exc
    with psycopg.connect(DATABASE_URL) as conn:
        did = insert_embedding(conn, content_hash, model, vector)
        conn.commit()
    return "embedded" if did else "lost-race"


@celery_app.task(name="worker.tasks.sweep_unembedded", bind=True)
def sweep_unembedded(self, batch: int = 50) -> str:
    """Beat backstop for embeddings, the exact mirror of sweep_unrated. The scrape path already
    enqueues an embed_signal per new row; this catches whatever slipped through (embeds that
    exhausted their retries, signals inserted via the API, or the whole backlog the first time
    EMBEDDING_MODEL gets set on an already-populated database). Bounded batch per tick so a
    backlog drains gradually instead of flooding the queue.

    Only real content (source = 'instagram') is embedded, same bill-not-a-safety-net reasoning as
    sweep_unrated: silently embedding thousands of seeded drill signals is a cost, not a backstop."""
    model = default_embedding_model()
    if not model:
        return "disabled (EMBEDDING_MODEL not set)"
    with psycopg.connect(DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (r.content_hash) r.content_hash, r.payload->>'caption' AS caption "
            "FROM raw_signals r "
            "WHERE r.payload->>'source' = 'instagram' "
            "  AND NOT EXISTS (SELECT 1 FROM signal_embeddings se WHERE se.content_hash = r.content_hash) "
            "ORDER BY r.content_hash, r.captured_at DESC LIMIT %s",
            (batch,),
        ).fetchall()
    for content_hash, caption in rows:
        embed_signal.delay(content_hash, caption)
    return f"enqueued {len(rows)}"


# --- Module 5 note ----------------------------------------------------------------
#
# The digest agent no longer runs from this worker. It used to: run_digest_session created a
# Managed Agent session, held its event stream, and answered the get_rated_signals custom tool
# host-side. That custom tool was the fragile piece, bound to whatever process held the stream,
# so it couldn't survive an unattended run. Module 5's migration moved the trigger to a Managed
# Agents *deployment* (packages/agents/deployment.yaml) and the tool to a remote MCP server (api/mcp_server.py)
# the agent dials directly. So the whole babysitter, and its host-side custom-tool listener, is
# gone. Nothing here enqueues digest work anymore; the deployment does, server-side.
#
# What follows below (the scrape fan-out, the rating stage, the beat backstops) is unchanged.

# --- trigger (sent by the API, by name) ------------------------------------------
#
# This used to be start_run(), a plain function the API imported and ran in ITS process
# (row insert + chord build). The monorepo split killed that import: the API now writes
# the runs row itself and fires DISPATCH_RUN over the broker (see task_contract); the
# worker owns everything Celery from here down. name= is the contract, so the API never
# needs this module to exist.


@celery_app.task(name=DISPATCH_RUN)
def dispatch_run(run_id: int, mode: str = "live", limit: int = 5, model: str | None = None) -> dict:
    """Build and enqueue the fan-out chord for an already-created runs row.

    The API inserted the row (status 'queued') with total = the influencer count at that
    moment; we re-read the influencers here and true-up total in case the watchlist changed
    between the insert and this task running.

    model is the data-plane half of the rating design: which model rates this run's new
    signals rides the request and is stored on the row (so two runs can rate with two models
    and be diffed). None falls back to the worker's RATING_MODEL default; if that's unset
    too, the run scrapes without rating."""
    run_ts = datetime.now(UTC).isoformat()
    with psycopg.connect(DATABASE_URL) as conn:
        influencers = (
            conn.cursor(row_factory=dict_row)
            .execute("SELECT id, instagram_handle, last_scraped_at FROM influencers ORDER BY id")
            .fetchall()
        )
        conn.execute("UPDATE runs SET total = %s WHERE id = %s", (len(influencers), run_id))
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

    header = [scrape_influencer.s(run_id, inf, run_ts, mode, limit, model) for inf in payload_infs]
    chord(header)(finalize_run.s(run_id))
    return {"run_id": run_id, "total": len(payload_infs), "mode": mode, "model": model}
