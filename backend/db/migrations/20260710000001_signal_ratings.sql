-- Module 4: storage for the AI rating layer.
--
-- signal_ratings is keyed on content_hash, the INPUT to the model, not the model's output.
-- The model is non-deterministic (same caption can produce slightly different words twice),
-- so deduping on its answer would never dedupe; deduping on the input hash means each
-- distinct piece of content gets rated once, ever, no matter how many times a scrape or the
-- beat sweep re-encounters it. Same idempotency story as raw_signals, one level up.
--
-- No foreign key to raw_signals: its primary key is (id, captured_at) because it's
-- partitioned, and content_hash alone isn't unique there (the same post can legitimately
-- land under two influencers). The hash is the join key by convention, like a content-addressed
-- store (S3 keys don't FK to the things that reference them either).
--
-- runs.model records which model rated a run. It's the data-plane half of the design, model
-- selection rides the request (POST /runs {"model": ...}) and is stored here; env vars keep
-- only the default and the credentials. NULL means "the worker's default".

-- migrate:up
CREATE TABLE signal_ratings (
    content_hash text PRIMARY KEY,
    model        text NOT NULL,           -- provider/model that produced this rating
    relevance    real NOT NULL CHECK (relevance BETWEEN 0 AND 1),
    confidence   real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    topics       text[] NOT NULL DEFAULT '{}',
    summary      text NOT NULL,
    rated_at     timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE runs ADD COLUMN model text;

-- migrate:down
ALTER TABLE runs DROP COLUMN IF EXISTS model;
DROP TABLE IF EXISTS signal_ratings;
