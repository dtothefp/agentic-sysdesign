-- Module 4 visibility, part 2: give a run its own `rating` lifecycle state.
--
-- Before this, a run flipped straight to `completed` at scrape fan-in while its rate_signal
-- jobs were still draining, so `completed` meant "the data landed," not "everything's done."
-- That's a confusing contract. Now the lifecycle is queued -> running -> rating -> completed:
-- the fan-in refreshes the rollup and moves the run to `rating` (still non-blocking, a slow
-- model never gates the barrier), and the run reaches `completed` only when the last rating
-- lands (rated_count == inserted). A run with no rating work skips `rating` and completes at
-- fan-in as before.
--
-- The status CHECK is an inline column constraint, so Postgres auto-named it runs_status_check.
-- Widen it to admit 'rating'. The active-runs partial index also learns 'rating' counts as
-- in-progress, so a run mid-rating-drain still shows up in the "what's running now" lookup.

-- migrate:up
ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_status_check;
ALTER TABLE runs ADD CONSTRAINT runs_status_check
    CHECK (status IN ('queued', 'running', 'rating', 'completed', 'failed'));

DROP INDEX IF EXISTS runs_active_idx;
CREATE INDEX runs_active_idx ON runs (created_at DESC)
    WHERE status IN ('queued', 'running', 'rating');

-- migrate:down
ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_status_check;
ALTER TABLE runs ADD CONSTRAINT runs_status_check
    CHECK (status IN ('queued', 'running', 'completed', 'failed'));

DROP INDEX IF EXISTS runs_active_idx;
CREATE INDEX runs_active_idx ON runs (created_at DESC)
    WHERE status IN ('queued', 'running');
