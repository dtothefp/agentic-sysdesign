-- Reframe the domain from generic "competitors" to Defrag's influencer watchlist. The
-- shape is identical (an entity we track, and a partitioned firehose of signals about it),
-- so this is a rename, not a rebuild. Every ALTER below preserves the existing rows, which
-- is the point: you can `make migrate` on a running db and watch the schema move under the
-- data instead of dropping it.
--
-- The partitioning is untouched. RANGE-by-captured_at is exactly right for "scrape each
-- creator's posts since the last scrape", because a post's own publish time is the natural
-- partition key and monthly retention still applies.
--
-- Two facts this migration leans on:
--   * Renaming a column on a partitioned parent (raw_signals) cascades to every child
--     partition automatically, and the UNIQUE constraint + partition indexes follow the
--     rename. One ALTER, not one per partition.
--   * A materialized view stores its own output column names, so a base-column rename does
--     NOT rename the matview's output column. The matview has to be dropped and recreated
--     to carry influencer_id through. That's the one drop/create here.

-- migrate:up

-- 1. drop the matview first so it isn't holding a reference while we rename the base column.
DROP MATERIALIZED VIEW IF EXISTS daily_signal_rollup;

-- 2. the entity table: competitors -> influencers, with its constraints renamed to match so
--    the table doesn't read as half-renamed.
ALTER TABLE competitors RENAME TO influencers;
ALTER TABLE influencers RENAME CONSTRAINT competitors_pkey TO influencers_pkey;
ALTER TABLE influencers RENAME CONSTRAINT competitors_name_key TO influencers_name_key;

-- 3. influencer-specific columns. instagram_handle is the natural key we scrape by;
--    backfill it from name so the NOT NULL holds against existing seeded rows, then lock it.
--    domain was a competitor-era field with no influencer meaning, so it goes.
ALTER TABLE influencers ADD COLUMN instagram_handle TEXT;
UPDATE influencers SET instagram_handle = lower(name) WHERE instagram_handle IS NULL;
ALTER TABLE influencers ALTER COLUMN instagram_handle SET NOT NULL;
ALTER TABLE influencers ADD CONSTRAINT influencers_instagram_handle_key UNIQUE (instagram_handle);
ALTER TABLE influencers DROP COLUMN domain;

-- last_scraped_at is the incremental-scrape watermark. NULL means never scraped, so the
-- first run pulls only the most recent post; after that the scraper pulls posts newer than
-- this timestamp and advances it. It lives on the influencer because Module 1 is Instagram
-- only; if we add TikTok/YouTube later it moves to a per-platform sources row.
ALTER TABLE influencers ADD COLUMN last_scraped_at TIMESTAMPTZ;

-- 4. the foreign-key/column rename everywhere signals and their derived rows point back to
--    the entity. On raw_signals this single statement rewrites the column across all child
--    partitions and inside the UNIQUE (…, content_hash, captured_at) key.
ALTER TABLE sources     RENAME COLUMN competitor_id TO influencer_id;
ALTER TABLE raw_signals RENAME COLUMN competitor_id TO influencer_id;
ALTER TABLE events      RENAME COLUMN competitor_id TO influencer_id;
ALTER TABLE embeddings  RENAME COLUMN competitor_id TO influencer_id;
ALTER TABLE digests     RENAME COLUMN competitor_id TO influencer_id;

-- 5. the per-partition composite indexes were named *_comp_cap. The column they cover is now
--    influencer_id, and that index name shows up in EXPLAIN output, so rename them to match
--    or the drills read as lying. (2026_08 exists because migration 2 provisioned it.)
ALTER INDEX raw_signals_2026_05_comp_cap RENAME TO raw_signals_2026_05_inf_cap;
ALTER INDEX raw_signals_2026_06_comp_cap RENAME TO raw_signals_2026_06_inf_cap;
ALTER INDEX raw_signals_2026_07_comp_cap RENAME TO raw_signals_2026_07_inf_cap;
ALTER INDEX raw_signals_2026_08_comp_cap RENAME TO raw_signals_2026_08_inf_cap;

-- 6. the maintenance function builds future partitions, so it has to emit the new column
--    name and the new index suffix for every month it provisions from here on.
CREATE OR REPLACE FUNCTION create_month_partition(d date)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  start_ts date := date_trunc('month', d)::date;
  end_ts   date := (date_trunc('month', d) + interval '1 month')::date;
  part     text := 'raw_signals_' || to_char(start_ts, 'YYYY_MM');
BEGIN
  EXECUTE format(
    'CREATE TABLE IF NOT EXISTS %I PARTITION OF raw_signals FOR VALUES FROM (%L) TO (%L)',
    part, start_ts, end_ts);
  EXECUTE format(
    'CREATE INDEX IF NOT EXISTS %I ON %I (influencer_id, captured_at)',
    part || '_inf_cap', part);
END $$;

-- 7. rebuild the read-path rollup with the influencer_id column name.
CREATE MATERIALIZED VIEW daily_signal_rollup AS
SELECT influencer_id,
       date_trunc('day', captured_at) AS day,
       count(*) AS signal_count,
       count(DISTINCT source_id) AS source_count
FROM raw_signals
GROUP BY influencer_id, date_trunc('day', captured_at);
CREATE UNIQUE INDEX ON daily_signal_rollup (influencer_id, day);

-- migrate:down

DROP MATERIALIZED VIEW IF EXISTS daily_signal_rollup;

CREATE OR REPLACE FUNCTION create_month_partition(d date)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  start_ts date := date_trunc('month', d)::date;
  end_ts   date := (date_trunc('month', d) + interval '1 month')::date;
  part     text := 'raw_signals_' || to_char(start_ts, 'YYYY_MM');
BEGIN
  EXECUTE format(
    'CREATE TABLE IF NOT EXISTS %I PARTITION OF raw_signals FOR VALUES FROM (%L) TO (%L)',
    part, start_ts, end_ts);
  EXECUTE format(
    'CREATE INDEX IF NOT EXISTS %I ON %I (competitor_id, captured_at)',
    part || '_comp_cap', part);
END $$;

ALTER INDEX raw_signals_2026_08_inf_cap RENAME TO raw_signals_2026_08_comp_cap;
ALTER INDEX raw_signals_2026_07_inf_cap RENAME TO raw_signals_2026_07_comp_cap;
ALTER INDEX raw_signals_2026_06_inf_cap RENAME TO raw_signals_2026_06_comp_cap;
ALTER INDEX raw_signals_2026_05_inf_cap RENAME TO raw_signals_2026_05_comp_cap;

ALTER TABLE digests     RENAME COLUMN influencer_id TO competitor_id;
ALTER TABLE embeddings  RENAME COLUMN influencer_id TO competitor_id;
ALTER TABLE events      RENAME COLUMN influencer_id TO competitor_id;
ALTER TABLE raw_signals RENAME COLUMN influencer_id TO competitor_id;
ALTER TABLE sources     RENAME COLUMN influencer_id TO competitor_id;

ALTER TABLE influencers DROP COLUMN last_scraped_at;
ALTER TABLE influencers ADD COLUMN domain TEXT;
ALTER TABLE influencers DROP CONSTRAINT influencers_instagram_handle_key;
ALTER TABLE influencers DROP COLUMN instagram_handle;

ALTER TABLE influencers RENAME CONSTRAINT influencers_name_key TO competitors_name_key;
ALTER TABLE influencers RENAME CONSTRAINT influencers_pkey TO competitors_pkey;
ALTER TABLE influencers RENAME TO competitors;

CREATE MATERIALIZED VIEW daily_signal_rollup AS
SELECT competitor_id,
       date_trunc('day', captured_at) AS day,
       count(*) AS signal_count,
       count(DISTINCT source_id) AS source_count
FROM raw_signals
GROUP BY competitor_id, date_trunc('day', captured_at);
CREATE UNIQUE INDEX ON daily_signal_rollup (competitor_id, day);
