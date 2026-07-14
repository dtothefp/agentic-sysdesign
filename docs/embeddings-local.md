# Embeddings, and how to run semantic search locally

Written plainly because embeddings are easy to lose the thread on. If future-you is confused
again, read this top to bottom.

## What an embedding is (the one idea)

An embedding is a list of numbers that stands in for the *meaning* of a piece of text. Here it's
1536 numbers per caption. Think of each caption as a **point in space**. Similar meanings land
near each other, unrelated meanings land far apart.

You search by embedding the query the same way, then asking Postgres "which stored points are
nearest to this one?" Nearness is similarity of meaning. Nobody ever reads the 1536 numbers. The
database just measures distance between points (the `<=>` operator is cosine distance).

The payoff, seen live. A search for **"model context protocol"** returns a post that says
**"RAG is out, agentic retrieval is in"**. Zero shared words. Lexical (keyword) search misses it
completely. Semantic search finds it because the meanings sit close together. That's the entire
reason embeddings are in this app. See [module-6.md](module-6.md) for how the lexical and
semantic halves get fused with RRF.

## Where embeddings come from (providers)

Embeddings are produced by a model, same as chat, but a *different kind* of model.

- **Anthropic does not do embeddings.** Claude is chat/reasoning only. There is no Anthropic
  embeddings endpoint, so your Claude key cannot produce these.
- **Many providers can.** OpenAI, Voyage AI, Cohere, Google (Gemini), Mistral, Together, plus
  fully-local options (Ollama, sentence-transformers) that cost nothing and never leave the box.

This app uses **`openai/text-embedding-3-small`** for two reasons that are constraints, not taste.

1. **The DB column is `vector(1536)`.** `text-embedding-3-small` is natively 1536-wide, an exact
   fit. A model that returns another width (Ollama's `nomic-embed-text` is 768) can't fill the
   column without a schema migration plus HNSW index rebuild.
2. **The code speaks the OpenAI-compatible wire format** (`POST {base_url}/embeddings`), which
   Voyage, Together, and Mistral all cloned. So switching provider is an env change, never a code
   change. Same "own the interface, rent the model" rule as the rating layer.

## The two gotchas that make local semantic search look "broken"

Both are cases where the code works exactly as designed but you see `semantic: false` or empty
results and think it's broken. It isn't.

### Gate 1, no embedding provider set (the dimension constraint)

If `EMBEDDING_MODEL` is unset, the whole embedding path is **inert on purpose**. Search silently
runs lexical-only and every result reports `"semantic": false`. That's the safe default, not a
bug. To turn it on you need a provider that returns **1536-wide** vectors. That rules out plain
Ollama (768) unless you migrate the schema. `text-embedding-3-small` fits, so it needs
`OPENAI_API_KEY` **with billing or credit**. A valid key with a $0 balance returns HTTP 429
`insufficient_quota` (the exact wall hit on first setup, fixed by adding credit in the OpenAI
billing settings).

### Gate 2, the beat sweep only embeds real scrapes (`source='instagram'`)

Even with a provider set, the automatic backstop `worker.tasks.sweep_unembedded` embeds **only
`source='instagram'`** rows. That's a deliberate cost guard. Silently embedding thousands of
`seed` or `demo` signals is spend, not a safety net. So locally, where all your data is
`source='seed'` or `source='demo'`, the sweep touches nothing and search never goes semantic even
though everything is configured. This is the confusing one.

The fix is a **one-time manual backfill** that ignores the source filter (below).

## How to actually run it locally (the recipe)

1. In the sysdesign repo-root `.env` (gitignored, never committed):

   ```
   EMBEDDING_MODEL=openai/text-embedding-3-small
   OPENAI_API_KEY=sk-...                 # must have credit, else 429 insufficient_quota
   ```

2. Get some captioned content. `seed` signals have no caption, so they embed nothing useful.
   Either run a `demo` scrape (synthetic but captioned) or a `live` scrape (real IG captions):

   ```
   # via the agent tool / API: trigger_run(mode="demo")   or   mode="live"
   ```

3. Backfill embeddings for everything captioned (the sweep won't, for `seed` or `demo`):

   ```
   moon run worker:embed-backfill                         # all captioned, un-embedded signals
   # or, to preview the count first:
   uv run --package sysdesign-worker python -m worker.backfill --dry-run
   # from the host into the container db, add: --dsn postgresql://lab:lab@localhost:5432/sysdesign
   ```

   Backfill is synchronous (no worker or redis needed) and idempotent (`ON CONFLICT DO NOTHING`),
   so re-running only fills gaps. It aborts loudly on the first embed failure (bad key, no quota,
   wrong dimension) rather than grinding through hundreds of identical errors. Going forward, new
   `instagram` scrapes get embedded automatically by the scrape path plus the beat sweep. Backfill
   is only for the initial turn-on and for `seed` or `demo` data.

4. Verify. Any hit reporting `"semantic": true` with a `sources` list containing `semantic` means
   it's live:

   ```
   GET /search?q=evals          # or the search_signals agent tool
   ```

## Cost

`text-embedding-3-small` is ~$0.02 per million tokens. A caption is tens of tokens, so backfilling
the entire local corpus is a fraction of a cent. This is not where money goes. The chat and rating
models are ~100x pricier per call, which is exactly why the embedding-backed rating cache exists.
