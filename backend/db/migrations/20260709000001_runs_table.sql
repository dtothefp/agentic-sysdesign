-- Module 2: the `runs` table, the durable record of a fan-out scrape job.
--
-- Why a table and not just Redis. Redis carries live progress deltas for the SSE stream,
-- but pub/sub only delivers to whoever is subscribed at the moment a message is published.
-- A browser that connects late, or reconnects after a refresh, has missed those messages.
-- So the authoritative state (how many of N influencers are done, did it fail, when) lives
-- here in Postgres. The SSE endpoint reads this row for the snapshot on connect, THEN
-- subscribes to Redis for anything newer. That's what makes progress survive a refresh.
--
-- One row per triggered run. The fan-out enqueues N per-influencer Celery tasks; each one
-- bumps done_count as it lands its signals; the fan-in chord callback flips status to
-- 'completed' after it refreshes the rollup. Cost is trivial (one short row per run), so no
-- partitioning here, unlike raw_signals.

-- migrate:up
CREATE TABLE runs (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- queued  -> row created, tasks enqueued, nothing done yet
    -- running -> at least one per-influencer task has reported progress
    -- completed / failed -> fan-in ran (or a task errored fatally)
    status      text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    mode        text NOT NULL DEFAULT 'live'
                CHECK (mode IN ('live', 'demo')),  -- demo = synthetic signals, no Apify spend
    total       integer NOT NULL,                  -- how many influencers this run fans out to
    done_count  integer NOT NULL DEFAULT 0,        -- how many have finished (drives the % bar)
    inserted    integer NOT NULL DEFAULT 0,        -- signals actually inserted (ON CONFLICT misses excluded)
    error       text,                              -- populated when status = 'failed'
    created_at  timestamptz NOT NULL DEFAULT now(),
    started_at  timestamptz,                       -- first task reported
    finished_at timestamptz                        -- fan-in completed
);

-- the dashboard lists recent runs newest-first; a partial index keeps the "is anything
-- running right now" lookup cheap without scanning finished history.
CREATE INDEX runs_active_idx ON runs (created_at DESC) WHERE status IN ('queued', 'running');

-- migrate:down
DROP TABLE IF EXISTS runs;
