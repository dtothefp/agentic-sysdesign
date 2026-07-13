-- Module 6: hybrid search storage. Two retrieval indexes over signal content that the
-- /search endpoint fuses with Reciprocal Rank Fusion (RRF):
--   1. LEXICAL   full-text search, a generated tsvector column on raw_signals + a GIN index.
--   2. SEMANTIC  pgvector, a content-addressed signal_embeddings table + an HNSW index.
--
-- The lexical half needs nothing external and works the moment this lands. The semantic half
-- stays inert until an embedding provider fills signal_embeddings (EMBEDDING_MODEL set), the
-- same inert-until-keyed contract as the Module 4 rating layer. RRF fuses the two ranked lists
-- by ordinal rank alone, so it doesn't matter that a ts_rank and a cosine distance live on
-- totally different scales.

-- migrate:up

-- 1. LEXICAL. A STORED generated tsvector of each post's caption, maintained by Postgres on
--    every insert/update so the search vector can never drift from the payload it summarizes.
--
--    The 2-arg to_tsvector('english'::regconfig, ...) is IMMUTABLE, which a generated column
--    requires. The 1-arg to_tsvector(text) form depends on the session's
--    default_text_search_config and is therefore only STABLE, and Postgres rejects it here.
--    coalesce(..., '') keeps a caption-less signal (an empty tsvector) rather than NULL.
--
--    Adding the column to the partitioned parent cascades to every child partition in one
--    statement. It rewrites the table to populate existing rows, which is instant at this data
--    size (a few thousand rows) and a brief lock at prod's (also small); do it off-peak there.
ALTER TABLE raw_signals
  ADD COLUMN caption_tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('english', coalesce(payload->>'caption', ''))) STORED;

-- GIN is the inverted index that makes `caption_tsv @@ tsquery` fast: it maps each lexeme to
-- the rows containing it, the same structure a search engine's posting list uses. Created on
-- the partitioned parent, so it's a partitioned index (one child index per existing partition).
-- Any partition created LATER auto-gets a matching child index because a partitioned index on
-- the parent attaches to new partitions automatically, so create_month_partition needs no edit.
CREATE INDEX raw_signals_caption_tsv_gin ON raw_signals USING gin (caption_tsv);

-- 2. SEMANTIC. Content-addressed embeddings, keyed on content_hash exactly like signal_ratings:
--    no foreign key to the partitioned raw_signals (its PK is (id, captured_at) and content_hash
--    alone isn't unique there), the hash is the by-convention join key, one row per distinct
--    piece of content. Dedup on the INPUT, same idempotency story as ratings and raw_signals.
CREATE TABLE signal_embeddings (
    content_hash text PRIMARY KEY,
    model        text NOT NULL,           -- provider/model that produced this vector
    embedding    vector(1536) NOT NULL,   -- 1536 = OpenAI text-embedding-3-small, the schema's fixed dim
    embedded_at  timestamptz NOT NULL DEFAULT now()
);

-- HNSW is the graph-based approximate-nearest-neighbor index pgvector uses. It builds a
-- navigable small-world graph over the vectors and walks it in ~log(n) hops, instead of
-- scanning every row (which is what a query does with no index, or with the exact-but-slow
-- alternative). vector_cosine_ops because search embeds queries and documents the same way and
-- ranks by cosine. Unlike the GIN above, HNSW is APPROXIMATE (recall < 100%), the accepted
-- trade for sublinear search; ef_search tunes the recall/latency dial at query time.
CREATE INDEX signal_embeddings_hnsw ON signal_embeddings USING hnsw (embedding vector_cosine_ops);

-- migrate:down
DROP INDEX IF EXISTS signal_embeddings_hnsw;
DROP TABLE IF EXISTS signal_embeddings;
DROP INDEX IF EXISTS raw_signals_caption_tsv_gin;
ALTER TABLE raw_signals DROP COLUMN IF EXISTS caption_tsv;
