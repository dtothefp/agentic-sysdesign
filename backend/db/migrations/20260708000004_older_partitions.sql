-- Backfill partition coverage into the recent past. The initial partitions start at
-- 2026-05-01, so any post published before then has no child partition and the API rejects
-- its insert with a 400 (no partition covers the row's month). That's not a bug, it's the
-- RANGE partition contract: a row only lands if a partition covers its captured_at.
--
-- Real scrapes surface this immediately. Instagram lets a creator PIN an old post to the top
-- of their grid, and the scraper reads grid order, not date order, so the "most recent" post
-- can be months or years old. rpn's top grid slot is a pinned 2025-11 post, which is why the
-- first-run (resultsLimit=1) pull for rpn skipped: its newest-by-grid post predates every
-- partition. Backfilling Jan..Apr 2026 gives this-year posts somewhere to land. Pre-2026 pins
-- (rpn also has a 2024-07 one) stay uncovered on purpose; provisioning two years of empty
-- monthly partitions to catch a stale intro post isn't worth it.
--
-- These reuse create_month_partition (from migration 2, updated in migration 3), so each new
-- child gets the same (influencer_id, captured_at) composite index named *_inf_cap that the
-- drills expect. Idempotent: CREATE TABLE IF NOT EXISTS, so re-running is a no-op.

-- migrate:up
SELECT create_month_partition('2026-01-01');
SELECT create_month_partition('2026-02-01');
SELECT create_month_partition('2026-03-01');
SELECT create_month_partition('2026-04-01');

-- migrate:down
-- dropping a child partition drops its index with it.
DROP TABLE IF EXISTS raw_signals_2026_04;
DROP TABLE IF EXISTS raw_signals_2026_03;
DROP TABLE IF EXISTS raw_signals_2026_02;
DROP TABLE IF EXISTS raw_signals_2026_01;
