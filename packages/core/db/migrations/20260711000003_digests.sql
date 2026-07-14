-- Module 5: storage for agent-written digests.
--
-- The Module 1 initial schema pre-created a `digests` placeholder (one jsonb row per
-- influencer per day) before the digest design existed. Module 5 as actually built is
-- one weekly markdown DOCUMENT for the whole feed, written by the Managed Agent, so
-- the shapes don't reconcile. The placeholder never received a row in any environment
-- (verified: 0 rows local and prod), so this migration replaces it instead of
-- contorting ALTERs around a table nothing ever used.
--
-- One row per digest run, created by POST /digests BEFORE the agent session starts
-- (same row-first pattern as runs: the id is the handle everything else hangs off).
-- The lifecycle is queued -> running -> completed | failed, but unlike runs the
-- terminal flip doesn't come from a worker counter. It comes from the AGENT itself
-- calling PUT /digests/{id}/content from inside its sandbox; the worker only marks
-- `failed` if the session ends without that delivery having happened.
--
-- content_md is the digest markdown, stored inline rather than as a file pointer.
-- Digests are small (<600 words by prompt contract) and the row IS the product the
-- UI reads back, so a text column beats a detour through object storage.

-- migrate:up
DROP TABLE digests;

CREATE TABLE digests (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status       text NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    session_id   text,          -- Anthropic session id, joins the row to the Console trace
    content_md   text,          -- the digest itself, delivered by the agent over PUT
    word_count   int,
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

-- migrate:down
DROP TABLE IF EXISTS digests;

CREATE TABLE digests (
    id            BIGSERIAL PRIMARY KEY,
    influencer_id BIGINT NOT NULL,
    digest_date   DATE NOT NULL,
    body          JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (influencer_id, digest_date)
);
