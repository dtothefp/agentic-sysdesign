-- Initial schema. Every later module (Celery, AWS, pgvector, LLM, graph) reuses this,
-- so it is the single source of truth for the shape. The load-bearing bits: raw_signals
-- is RANGE partitioned by captured_at, and the partition key lives inside every unique
-- constraint (Postgres enforces uniqueness per partition, so the partition column has to
-- be in the key).

-- migrate:up
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE competitors (
  id          BIGSERIAL PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  domain      TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sources (
  id            BIGSERIAL PRIMARY KEY,
  competitor_id BIGINT NOT NULL REFERENCES competitors(id),
  kind          TEXT NOT NULL,          -- 'linkedin' | 'reddit' | 'changelog'
  url           TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- append-only raw scraped items, RANGE partitioned by captured_at.
-- the partition key must be in every unique constraint, hence it is in the PK.
CREATE TABLE raw_signals (
  id            BIGSERIAL,
  competitor_id BIGINT NOT NULL,
  source_id     BIGINT,
  captured_at   TIMESTAMPTZ NOT NULL,
  content_hash  TEXT NOT NULL,          -- sha256 of payload, for idempotent dedup
  payload       JSONB NOT NULL,
  PRIMARY KEY (id, captured_at),
  UNIQUE (competitor_id, content_hash, captured_at)
) PARTITION BY RANGE (captured_at);

CREATE TABLE events (
  id            BIGSERIAL PRIMARY KEY,
  competitor_id BIGINT NOT NULL REFERENCES competitors(id),
  raw_signal_id BIGINT,
  kind          TEXT NOT NULL,          -- 'release'|'breach'|'exec_change'|'funding'|'other'
  occurred_at   TIMESTAMPTZ NOT NULL,
  summary       TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE embeddings (
  id            BIGSERIAL PRIMARY KEY,
  competitor_id BIGINT NOT NULL,
  ref_kind      TEXT NOT NULL,          -- 'raw_signal'|'event'
  ref_id        BIGINT NOT NULL,
  content       TEXT NOT NULL,
  embedding     VECTOR(1536),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE digests (
  id            BIGSERIAL PRIMARY KEY,
  competitor_id BIGINT NOT NULL,
  digest_date   DATE NOT NULL,
  body          JSONB NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (competitor_id, digest_date)
);

-- durable job-of-record for one pipeline run. this row survives a page refresh, a
-- worker crash, and a Redis flush, which the ephemeral per-step progress in Redis
-- does not. Module 2 writes to it on state transitions and pushes high-frequency
-- progress to a Redis cache key instead of updating this row on every tick.
CREATE TABLE runs (
  id            BIGSERIAL PRIMARY KEY,
  trigger       TEXT NOT NULL,          -- 'manual'|'schedule'|'webhook'
  status        TEXT NOT NULL DEFAULT 'started',  -- 'started'|'running'|'completed'|'failed'|'cancelled'
  stats         JSONB,                  -- final counts, written once at a terminal state, not per tick
  started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at  TIMESTAMPTZ            -- null while running
);

CREATE MATERIALIZED VIEW daily_signal_rollup AS
SELECT competitor_id,
       date_trunc('day', captured_at) AS day,
       count(*) AS signal_count,
       count(DISTINCT source_id) AS source_count
FROM raw_signals
GROUP BY competitor_id, date_trunc('day', captured_at);
CREATE UNIQUE INDEX ON daily_signal_rollup (competitor_id, day);

-- migrate:down
DROP MATERIALIZED VIEW IF EXISTS daily_signal_rollup;
DROP TABLE IF EXISTS runs;
DROP TABLE IF EXISTS digests;
DROP TABLE IF EXISTS embeddings;
DROP TABLE IF EXISTS events;
DROP TABLE IF EXISTS raw_signals;
DROP TABLE IF EXISTS sources;
DROP TABLE IF EXISTS competitors;
DROP EXTENSION IF EXISTS vector;
