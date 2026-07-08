-- Monthly RANGE child partitions for raw_signals, plus an idempotent maintenance
-- function that provisions the month containing a given date. In production this job
-- goes to pg_partman (retention window + pre-created future partitions); the hand-rolled
-- function here shows you understand the mechanism. Children must exist before seeding,
-- so this runs after the initial schema and before the seed step.

-- migrate:up
CREATE TABLE IF NOT EXISTS raw_signals_2026_05
  PARTITION OF raw_signals FOR VALUES FROM ('2026-05-01') TO ('2026-06-01');
CREATE TABLE IF NOT EXISTS raw_signals_2026_06
  PARTITION OF raw_signals FOR VALUES FROM ('2026-06-01') TO ('2026-07-01');
CREATE TABLE IF NOT EXISTS raw_signals_2026_07
  PARTITION OF raw_signals FOR VALUES FROM ('2026-07-01') TO ('2026-08-01');

-- per-partition composite index, one per child. competitor first (equality predicate),
-- captured_at second (range predicate).
CREATE INDEX IF NOT EXISTS raw_signals_2026_05_comp_cap
  ON raw_signals_2026_05 (competitor_id, captured_at);
CREATE INDEX IF NOT EXISTS raw_signals_2026_06_comp_cap
  ON raw_signals_2026_06 (competitor_id, captured_at);
CREATE INDEX IF NOT EXISTS raw_signals_2026_07_comp_cap
  ON raw_signals_2026_07 (competitor_id, captured_at);

-- idempotent maintenance: provision the month containing `d`
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

-- provision next month before it arrives (what a scheduled task calls monthly)
SELECT create_month_partition('2026-08-15');

-- migrate:down
DROP FUNCTION IF EXISTS create_month_partition(date);
DROP TABLE IF EXISTS raw_signals_2026_08;
DROP TABLE IF EXISTS raw_signals_2026_07;
DROP TABLE IF EXISTS raw_signals_2026_06;
DROP TABLE IF EXISTS raw_signals_2026_05;
