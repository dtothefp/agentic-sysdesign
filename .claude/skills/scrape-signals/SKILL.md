---
name: scrape-signals
description: Populate the sysdesign raw_signals table with real Defrag influencer data by scraping their recent Instagram posts (Apify REST API) and POSTing them through the FastAPI backend. Incremental, all influencers in parallel, driven by each influencer's last_scraped_at watermark. Use when you want real data in the database to run the EXPLAIN drills against, or to refresh the watchlist's recent posts. Trigger phrases include "scrape the influencers", "scrape instagram signals", "populate the db with real data", "refresh the watchlist".
---

# scrape-signals

Fills `raw_signals` with the Defrag influencer watchlist's real Instagram posts by going
through the API, not by writing to Postgres directly. Every insert takes the same idempotent
`ON CONFLICT DO NOTHING` path the seed and the app use, so the data-integrity story stays
honest.

This is Module 1's Phase B. It turns the synthetic seed into a real firehose so the drills
mean something and the later topic-judging (Claude / OpenAI / Anthropic relevance) has real
captions to score.

## How it works

`scrape_ig.py` scrapes every influencer returned by `GET /influencers`, in parallel, and is
incremental per influencer via the `last_scraped_at` watermark:

- **First run** (watermark NULL): pulls only the single most recent post per influencer.
- **Later runs**: pulls posts published after the watermark, then advances it to the run time.

It uses the Apify REST `run-sync-get-dataset-items` endpoint (actor `apify/instagram-scraper`),
so no MCP and no SDK. The signal payload is the post's stable identity (shortcode, url, type,
caption, posted_at), deliberately excluding volatile metrics and expiring CDN urls, so
re-scraping the same post is a no-op.

## Prerequisites

1. The db is up and migrated + seeded. From `backend/`: `make setup` (host) or `make db-init`
   (dev container). The seed loads the 5-influencer watchlist, so they exist to scrape.
2. `APIFY_API_KEY` is in `backend/.env` (gitignored). The scraper reads it from there or the env.
3. The API is running. From `backend/`: `make api`. Confirm with `curl -s localhost:8000/health`.

## Run it

From `backend/`, the simple path:

```bash
make scrape        # all watchlist influencers, in parallel
```

Or directly, for more control (from the repo root):

```bash
uv run --project backend python .claude/skills/scrape-signals/scrape_ig.py                 # all
uv run --project backend python .claude/skills/scrape-signals/scrape_ig.py --handle nick_saraev --limit 30
```

It prints a per-influencer line of `posts / inserted / already-had / skipped`. Run it twice
back to back: the second run should insert ~0 (nothing new since the watermark advanced).
That's the incremental + idempotency contract working, and a clean thing to show in an interview.

## The captured_at rule (read before scraping older data)

`raw_signals` is RANGE partitioned by `captured_at` (the post's own publish time), and a row
only inserts if a partition covers its month. So:

- Recent posts land in the current month's partition and insert fine.
- Posts older than the oldest partition are rejected by the API (400) and counted as skipped.
  That's the partitioning lesson, not a bug. To ingest older posts, provision the month first
  with a migration that calls `create_month_partition('<any date in that month>')`.

## Adding another source (TikTok, YouTube, a changelog)

`scrape_ig.py` is the Instagram implementation; `scrape_hn.py` is a minimal template for any
source that exposes clean JSON. A new scraper needs to:

1. Fetch from the source.
2. Shape each item into a stable `payload` dict (identity + content, not volatile metrics).
   Keep a `source` key so you can tell scrapers apart.
3. Pick `captured_at` (the item's publish time keeps re-runs idempotent).
4. POST to `/signals`. The API derives `content_hash` from the payload, so you never compute
   it client-side. Advance the influencer watermark via `PATCH /influencers/{id}` if the source
   is per-influencer.

## Driving it with Claude Code directly

For a one-off or a source without a clean API, skip the script: fetch the page yourself,
extract the items, and POST each to `/signals`. The script is just the repeatable path.
