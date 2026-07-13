# Module 6: Hybrid Search (lexical + semantic, fused with RRF)

The interview one-liner. **Run keyword search and vector search in parallel, then fuse the two
ranked lists by rank position, not by score.** Everything below is why each piece is there and
what it's protecting against. Written to be talked through out loud, because the whole point of
this module is being able to explain Postgres search cold.

The corpus is signal captions (the text of tracked creators' posts). The feature is one endpoint,
`GET /search?q=...`, and one MCP tool, `search_signals`, that the digest agent can call to find
posts about a topic instead of only reading the rated-signals list.

---

## 1. Why hybrid at all, the two blind spots

Neither retrieval method is complete on its own. That's the entire justification, and it's the
first thing to say in an interview.

- **Lexical (keyword) search** matches *words*. Query "autonomous agents" will not find a post
  that says "self-directed LLM workflows", zero shared words, identical meaning. Lexical is blind
  to synonyms and paraphrase.
- **Semantic (vector) search** matches *meaning*. It nails the paraphrase case, but it's fuzzy on
  exact tokens. A rare product name, an acronym, a handle, an exact phrase the model never saw
  enough of to place meaningfully in embedding space. Semantic is blind to the literal.

```
query: "autonomous agents"

  lexical only ─────────►  finds "building autonomous agents"      ok
                           MISSES "self-directed LLM workflows"    no (no shared words)

  semantic only ────────►  finds "self-directed LLM workflows"     ok (close in meaning)
                           fuzzy on "ACME-7 agent" exact match     ~ (rare token)

  hybrid ───────────────►  finds BOTH, and ranks a doc found by
                           both methods above one found by only one
```

Production search stacks (Elasticsearch, OpenSearch, Weaviate) all ship hybrid for this reason.
This module builds it from primitives so the mechanism is visible instead of a black-box `hybrid:
true` flag.

---

## 2. The lexical half, Postgres full-text search

Three pieces. A `tsvector` column, a GIN index, and a ranked query. All standard Postgres, no
extension.

### 2a. The generated `tsvector` column

```sql
ALTER TABLE raw_signals
  ADD COLUMN caption_tsv tsvector
  GENERATED ALWAYS AS (to_tsvector('english', coalesce(payload->>'caption', ''))) STORED;
```

A `tsvector` is the caption pre-processed into **lexemes**. Lowercased, stop-words dropped,
words stemmed to a root. "Building autonomous LLM agents" becomes `'agent':4 'autonom':2
'build':1 'llm':3`. Storing this, not the raw text, is what makes matching fast and
grammar-insensitive ("agents" and "agent" both match the lexeme `agent`).

`GENERATED ALWAYS AS ... STORED` means Postgres computes and stores this column automatically on
every insert and update. The search vector can never drift from the caption it summarizes, because
the application never writes it, the database derives it. That's the payoff over a trigger or
app-side maintenance. One fewer thing that can be forgotten or get out of sync.

**The gotcha worth knowing (this is an interview trap).** A generated column's expression must be
`IMMUTABLE`, same inputs, same output, forever. The two-argument
`to_tsvector('english'::regconfig, ...)` is immutable. The one-argument `to_tsvector(text)` is only
`STABLE`, because it reads the session's `default_text_search_config` at runtime, so its output
could change with a session setting. Postgres rejects the one-arg form in a generated column. You
must name the config explicitly. `coalesce(..., '')` handles a caption-less signal as an empty
vector rather than NULL.

### 2b. The GIN index, an inverted index

```sql
CREATE INDEX raw_signals_caption_tsv_gin ON raw_signals USING gin (caption_tsv);
```

GIN (Generalized Inverted Index) is *the* structure search engines use. It maps each lexeme to
the list of rows containing it, a "posting list." To find every caption containing `agent`, you
read one posting list instead of scanning every row. That's what makes `caption_tsv @@ tsquery`
fast at scale.

`raw_signals` is **partitioned by month**, so this is a *partitioned index*. Creating it on the
parent creates one child index per existing partition, and the nice part, a partition created
*later* automatically gets its own matching child index. So `create_month_partition` needed no
edit. New months inherit the GIN for free.

### 2c. The ranked query

```sql
SELECT s.content_hash, ts_rank_cd(s.caption_tsv, q) AS score
FROM raw_signals s, websearch_to_tsquery('english', %s) q
WHERE s.caption_tsv @@ q
ORDER BY score DESC;
```

- `websearch_to_tsquery` parses a *user-style* query string. Bare words are AND-ed, `"quoted
  phrases"` are exact, `OR` and `-negation` work. It's the forgiving parser you want behind a
  search box (versus `to_tsquery`, which throws on unescaped punctuation).
- `@@` is the match operator, "does this tsvector satisfy this tsquery?" This is the predicate the
  GIN index serves.
- `ts_rank_cd` ranks the matches. The `cd` is **cover density**, it rewards matched terms
  appearing *close together*, not just frequently. "agent orchestration" scores higher on a
  caption where those words are adjacent than one where they're paragraphs apart.

---

## 3. The semantic half, pgvector + HNSW

```sql
CREATE TABLE signal_embeddings (
    content_hash text PRIMARY KEY,
    model        text NOT NULL,
    embedding    vector(1536) NOT NULL,   -- OpenAI text-embedding-3-small's native width
    embedded_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX signal_embeddings_hnsw ON signal_embeddings USING hnsw (embedding vector_cosine_ops);
```

An **embedding** is a fixed-length vector of floats that places text in a space where distance
equals dissimilarity. Caption and query are embedded by the *same* model, and nearby vectors mean
nearby meaning. `vector(1536)` is a fixed width, 1536 is text-embedding-3-small's size, and it's a
hard schema constraint. A 768-dim model (Ollama's `nomic-embed-text`) or a 1024-dim one can't fill
this column. Changing the width is a migration, because an HNSW index is built over a fixed
dimensionality.

**Keyed on `content_hash`, no foreign key.** Same design as `signal_ratings`. `raw_signals` is
partitioned and its PK is `(id, captured_at)`, so `content_hash` alone isn't a legal FK target.
The hash is the by-convention join key, one row per distinct piece of content. Dedup on the input.

### HNSW, the index that makes it sublinear

Without an index, "find the nearest vectors to this query" scans every row and computes every
distance, O(n), fine for thousands, dead at millions. **HNSW (Hierarchical Navigable Small
World)** builds a layered graph over the vectors and greedily walks it toward the query in about
log(n) hops.

The catch, and the one thing to flag out loud. **HNSW is APPROXIMATE.** It can miss a true nearest
neighbor (recall below 100%). That's the accepted trade for sublinear search, and it's tunable via
`hnsw.ef_search`, which sets how many candidates the walk keeps, trading recall for latency at
query time. This is the opposite of the GIN index, which is exact. So one half of the search is
exact and one is approximate, which is fine because RRF fuses them and the exact half backstops the
fuzzy one.

`vector_cosine_ops` plus the `<=>` operator gives cosine distance. `ORDER BY embedding <=> $query
LIMIT k` is the exact query shape HNSW accelerates.

---

## 4. Reciprocal Rank Fusion, combining the two lists

Now you have two ranked lists of `content_hash`. How do you merge them into one ranking?

**The wrong way, add or average the scores.** A `ts_rank_cd` lives on roughly 0.0 to 1.0. A cosine
distance lives on 0.0 to 2.0 and is *inverted* (smaller is better). They're different units on
different scales. Normalizing them into a shared scale is fiddly, corpus-dependent, and brittle.

**RRF's move, throw the scores away and fuse by RANK POSITION.** A document's contribution from
each list is `1 / (k + rank)`, summed across the lists it appears in.

```
fused_score(doc) = sum over lists containing doc of  1 / (k + rank_in_that_list)
```

```
lexical: [ h1, h7, h3, ... ]     h1 is rank 1 -> 1/(60+1) = 0.0164
semantic:[ h3, h1, h9, ... ]     h1 is rank 2 -> 1/(60+2) = 0.0161
                                 h1 total     = 0.0325   (found by BOTH, ranks high in both)
                                 h3 total     = 0.0164 (sem rank 1) + 1/(60+3) lexical
```

Two properties fall out, and they're the whole reason RRF is the default fusion in Elasticsearch
and OpenSearch.

1. **Scale-invariant.** Only ordinal position matters. A method's raw scores never enter the math,
   so mismatched score ranges can't bias the fusion. This is the headline win.
2. **Agreement is rewarded.** A doc that both methods rank highly beats a doc only one method
   found. Agreement across independent methods is the signal. That's exactly the corroboration you
   want, and it's why a hit found by both `lexical` and `semantic` sorts above one found by either
   alone.

`k` (60, the value from the original 2009 RRF paper, and what Elasticsearch ships) damps how much
the very top rank dominates. Large `k` flattens the `1/(k+rank)` curve so ranks 1 and 2 are nearly
equal. Small `k` makes rank 1 tower over everything. 60 is a sane default, not a tuned magic
number, say that.

The fusion is a **pure function** (`reciprocal_rank_fusion` in `common/search.py`). Ranked id
lists in, fused order out, no I/O. That's what makes it unit-testable without a database, and the
tests assert the closed-form score and the agreement property directly.

---

## 5. The architecture, three decoupled pipelines, one shared table

The embedding stage is its own pipeline, decoupled from scraping and rating exactly the way rating
was decoupled from scraping in Module 4. Scrape lands a signal, enqueues an `embed_signal` job,
that job embeds the caption and writes `signal_embeddings`. A slow embedding provider never stalls
a scrape or a rating.

```
scrape_influencer ──► inserts raw_signal ──┬──► rate_signal   (Module 4 -> signal_ratings)
                                           └──► embed_signal  (Module 6 -> signal_embeddings)

both write idempotently (ON CONFLICT DO NOTHING); whoever gets there first wins,
the other fast-skips. Beat sweeps (sweep_unrated, sweep_unembedded, every 10 min)
backstop anything that slipped through, same pattern as the matview refresh.
```

Everything is **inert until keyed**, the contract that keeps prod safe.

- `EMBEDDING_MODEL` unset means no embeddings computed, `search` runs **lexical-only** and still
  works. The response says `"semantic": false` so a caller *sees* the vector half didn't run
  rather than silently getting half a search.
- `EMBEDDING_MODEL` set means the semantic half turns on. Same switch the rating layer uses with
  `RATING_MODEL`.

The **provider adapter** (`common/embedding.py`) mirrors `common/rating.py` exactly. Raw urllib
against the OpenAI-compatible `/v1/embeddings` shape, "provider/model" strings, a `PROVIDERS`
registry (openai, ollama, together, voyage, mistral), fail-at-the-door resolution. Same design
rule, **own the interface, rent the model.** The one embedding-specific check, the returned
vector's width must equal 1536, or `embed_text` raises with a message naming the mismatch instead
of letting Postgres reject a wrong-width vector with an opaque error. The validator is the
contract, same philosophy as the rating parser.

Offline story. The *lexical* half needs nothing external, no model, no network. So on a laptop
with no embedding key, search still works, just keyword-only. The semantic half lights up the
moment a 1536-dim provider is configured.

---

## 6. Module 4's semantic cache, the same table, reused

This is the Module 4 piece that was pending, and it falls out of Module 6 almost for free, which
is the satisfying part.

**The idea.** Before paying for an LLM rating call, check whether a *near-identical* caption was
already rated. If so, copy that rating and skip the model. A reposted quote, a creator's
boilerplate CTA, the same announcement across accounts, all get rated once.

**The mechanism reuses the search embeddings.** To rate a caption, `rate_signal`:

1. Ensures the signal has an embedding (reuses the stored one if the search pipeline already
   embedded it, otherwise computes and stores it, one vector, two features).
2. Runs a KNN lookup for the nearest signal that *already has a rating*, using that stored
   embedding as the probe (the same `<=>` plus HNSW query the search uses).
3. If the nearest rated neighbor is within `RATING_CACHE_MAX_DISTANCE` (default 0.05 cosine
   distance, about 0.95 similarity, deliberately tight, this is for *duplicate-ish* content, not
   merely *similar* content), it copies that rating and tags it `cache:<model>` in
   `signal_ratings`. Otherwise it calls the model as usual.

```
new caption ──► embed (about 100x cheaper than a chat call)
             ──► KNN nearest ALREADY-RATED neighbor
                    within 0.05 distance? ──► copy its rating, tag cache:<model>   [HIT, no LLM call]
                    else                   ──► call the model                       [MISS]
```

The economics are the whole point. An embedding call is about 100x cheaper than a chat completion,
so paying for an embedding to *maybe* skip a chat call is a strict win on hits and a cheap miss
otherwise. And you can measure the hit rate directly, `SELECT count(*) FROM signal_ratings WHERE
model LIKE 'cache:%'`. It degrades safely. If the embedding provider hiccups, the lookup returns
"no cache" and the signal is rated normally.

The soundbite. **The vector you compute for search doubles as the dedup key for rating.** One
`signal_embeddings` table, two features.

---

## 7. Surfaces

- `GET /search?q=...&limit=` returns `{query, semantic, hits[]}`. Each hit carries its fused
  `score`, a `sources` list (`["lexical","semantic"]` when both halves found it), the caption
  excerpt, and the Module 4 rating when present (a LEFT JOIN, search doesn't depend on a signal
  being rated).
- MCP tool `search_signals(query, limit)` returns the same result, for the digest agent. It shares
  `common/search.search_signals` with the endpoint's embed-then-fuse path so the tool and the REST
  route can't drift, the same anti-drift move `get_rated_signals` uses across the MCP and worker
  paths.

The API endpoint and the MCP tool make the identical "embed the query, or degrade to lexical"
decision via one shared `embed_query` function, so there's exactly one place that fallback lives.

---

## 8. What to demo, EXPLAIN drills

- `EXPLAIN` the vector query and point at `Index Scan using signal_embeddings_hnsw`, proof the
  HNSW index is used, not a seq scan.
- `EXPLAIN` the FTS query and point at the `Bitmap Index Scan` on `raw_signals_caption_tsv_gin`.
- Show a query where lexical and semantic disagree, then show RRF's fused order putting the
  both-methods hit on top.
- Flip `EMBEDDING_MODEL` off and rerun `/search`, `"semantic": false`, lexical results still land.
- After a rating run with embeddings on, `SELECT model, count(*) FROM signal_ratings GROUP BY
  model`, the `cache:%` rows are the semantic-cache hits.

---

## Interview soundbites (memorize these)

- "Hybrid search runs keyword and vector retrieval in parallel and fuses the two ranked lists.
  Each covers the other's blind spot, keyword is blind to synonyms, vectors are fuzzy on exact
  tokens."
- "I fuse with Reciprocal Rank Fusion, which combines by rank *position*, not score. That sidesteps
  normalizing a ts_rank against a cosine distance, two different scales, and it rewards documents
  both methods agree on."
- "The lexical side is a generated `tsvector` column plus a GIN inverted index. The generated
  column has to use the two-arg `to_tsvector` because a generated expression must be immutable and
  the one-arg form is only stable."
- "The vector side is pgvector with an HNSW index, approximate nearest neighbor, sublinear, recall
  traded for speed via `ef_search`. The keyword side is exact, so it backstops the approximate one."
- "Embeddings are one table serving two features, semantic search, and a rating cache that copies a
  prior rating when a new caption is a near-duplicate. An embedding is about 100x cheaper than the
  LLM call it saves."
- "The whole semantic layer is inert until an embedding model is configured. With none, search
  degrades to keyword-only and says so in the response."
