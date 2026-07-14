-- Module 1 EXPLAIN drills. This is the core of the module. Run each block with the
-- plan reader and read the tree top down. ANALYZE runs the query and reports real
-- row counts and timing; BUFFERS reports pages read, the honest cost signal.
--
--   psql "$DATABASE_URL" -f drills/explain-drills.sql
--
-- Or paste blocks one at a time into psql so you can watch each plan on its own.

-- (a) Influencer plus date range, pruned. The good path.
-- Expect: an Append node with a single child (raw_signals_2026_07), other months
-- absent, and an Index Scan using raw_signals_2026_07_inf_cap under it. The
-- predicate carries the partition key, so the planner prunes to one partition, then
-- the composite index resolves the influencer. One month touched, not three.
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM raw_signals
WHERE influencer_id = 3
  AND captured_at >= '2026-07-01' AND captured_at < '2026-08-01';

-- (b) Same query, no index. Feel the cost.
-- Real-Postgres nuance: dropping only the composite index is NOT enough to get a Seq
-- Scan here. Postgres falls back to the UNIQUE(influencer_id, content_hash,
-- captured_at) index, which also leads with influencer_id and carries captured_at, so
-- you still see an Index Scan on a wider key. That itself is a good interview point:
-- a unique constraint is also an index the planner can use. To actually feel the seq
-- scan cost, disable index plans for this one query. Partitioning still prunes to one
-- month; the index is what decides whether you read the bytes at all. Two levers.
DROP INDEX raw_signals_2026_07_inf_cap;
SET enable_indexscan = off;
SET enable_bitmapscan = off;
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM raw_signals
WHERE influencer_id = 3
  AND captured_at >= '2026-07-01' AND captured_at < '2026-08-01';
RESET enable_indexscan;
RESET enable_bitmapscan;
CREATE INDEX raw_signals_2026_07_inf_cap
  ON raw_signals_2026_07 (influencer_id, captured_at);

-- (c) No partition key in the predicate. The anti-pattern.
-- Expect: an Append with every child partition listed. Nothing to prune on, so it
-- fans out across all months. Filtering on influencer_id alone cannot prune a table
-- partitioned by captured_at. Always carry a time bound, or partition by the axis
-- you actually filter on (HASH by influencer_id, see the guide's stretch goals).
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM raw_signals WHERE influencer_id = 3;

-- (d) Index-only scan via a covering index. Cheapest read for a narrow projection.
-- Expect: Index Only Scan and Heap Fetches: 0 (after the VACUUM sets the visibility
-- map). When every column the query needs lives in the index, Postgres skips the
-- heap entirely. That is why INCLUDE columns matter for hot read paths.
CREATE INDEX IF NOT EXISTS raw_signals_2026_07_cover
  ON raw_signals_2026_07 (influencer_id, captured_at) INCLUDE (source_id);
VACUUM raw_signals_2026_07;
EXPLAIN (ANALYZE, BUFFERS)
SELECT influencer_id, captured_at, source_id FROM raw_signals
WHERE influencer_id = 3
  AND captured_at >= '2026-07-01' AND captured_at < '2026-08-01';

-- (e) Materialized view vs raw aggregate. The read/write split, made visible.
-- Expect: the matview read is an Index Scan over a small precomputed table; the raw
-- version is an Append plus HashAggregate over every partition. Writes land in
-- raw_signals, reads hit the rollup. The dashboard never pays for the aggregate.
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM daily_signal_rollup WHERE influencer_id = 3;

EXPLAIN (ANALYZE, BUFFERS)
SELECT influencer_id, date_trunc('day', captured_at) AS day,
       count(*), count(DISTINCT source_id)
FROM raw_signals WHERE influencer_id = 3
GROUP BY influencer_id, date_trunc('day', captured_at);

-- (f) Concurrent refresh. Refresh without locking readers.
-- Works only because the matview has a UNIQUE INDEX on (influencer_id, day). Without
-- CONCURRENTLY the refresh takes an ACCESS EXCLUSIVE lock and blocks reads for its
-- duration. The unique index is the enabler for online refreshes, not decoration.
REFRESH MATERIALIZED VIEW CONCURRENTLY daily_signal_rollup;
