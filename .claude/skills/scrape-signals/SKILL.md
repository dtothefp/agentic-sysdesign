---
name: scrape-signals
description: Populate the sysdesign database with real Defrag influencer data entirely through the FastAPI backend. Seed the watchlist via POST /influencers/bulk, GET the influencers back, scrape each one's recent Instagram posts (Apify REST), then POST each post to /signals. Incremental and parallel, driven by each influencer's last_scraped_at watermark. This is how Claude Code interacts with the app's database. Use when you want real data in the database to run the EXPLAIN drills against, or to refresh the watchlist's recent posts. Trigger phrases include "scrape the influencers", "scrape instagram signals", "seed the watchlist", "populate the db with real data", "refresh the watchlist".
---

# scrape-signals

Claude Code's way of driving the sysdesign database. Everything goes through the running
FastAPI on `localhost:8000`, never straight to Postgres, so every write takes the same
idempotent `ON CONFLICT DO NOTHING` path the app uses. That keeps the data-integrity story
honest and makes the skill a live demo of the API.

The loop is four steps:

1. **Seed the watchlist** through the API (`POST /influencers/bulk`).
2. **Get the influencers** back (`GET /influencers`), so we have their ids and watermarks.
3. **Scrape** each one's recent Instagram posts (Apify REST), all in parallel.
4. **Post the scraped posts** to `/signals` (and advance each watermark via `PATCH`).

Steps 2 to 4 are what `scrape_ig.py` does in one run. Step 1 is a single curl you run first.

## Prerequisites

1. The API is running. From the repo root: `moon run api:dev`. Confirm with `curl -s localhost:8000/health`.
2. The db is migrated. From the repo root: `moon run core:migrate` (or `moon run core:db-init`, which also seeds
   synthetic volume for the drills). Migration alone is enough for the scrape loop.
3. `APIFY_API_KEY` is in the repo-root `.env` (gitignored). `scrape_ig.py` reads it from there or the env.

## Step 1: seed the watchlist (curl)

The watchlist lives in `watchlist.json` next to this file (one source of truth). Load it all
at once:

```bash
curl -sX POST localhost:8000/influencers/bulk \
  -H 'Content-Type: application/json' \
  --data @.claude/skills/scrape-signals/watchlist.json | python3 -m json.tool
```

Or add a single creator:

```bash
curl -sX POST localhost:8000/influencers \
  -H 'Content-Type: application/json' \
  -d '{"name": "Nick Saraev", "instagram_handle": "nick_saraev"}'
```

Both upsert on `instagram_handle`, so re-running never duplicates a creator. (`moon run core:db-init`
seeds the same watchlist offline, reading the same `watchlist.json`, so if you ran that you
can skip this step.)

## Step 2 to 4: scrape and post (one run)

From the repo root, with the API running:

```bash
uv run python .claude/skills/scrape-signals/scrape_ig.py             # all influencers
uv run python .claude/skills/scrape-signals/scrape_ig.py --handle nick_saraev --limit 30
```

It GETs `/influencers`, scrapes each in parallel via the Apify REST
`run-sync-get-dataset-items` endpoint (actor `apify/instagram-scraper`, no MCP, no SDK), POSTs
each post to `/signals`, then PATCHes the watermark. It's incremental per influencer:

- **First run** (watermark NULL): pulls only the single most recent post per influencer.
- **Later runs**: pulls posts published after the watermark, then advances it to the run time.

The signal payload is the post's stable identity (shortcode, url, type, caption, posted_at),
deliberately excluding volatile metrics and expiring CDN urls, so re-scraping the same post
is a no-op.

It prints a per-influencer line of `posts / inserted / already-had / skipped`. Run it twice
back to back: the second run inserts ~0 (nothing new since the watermark advanced). That's the
incremental + idempotency contract working, and a clean thing to show in an interview.

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

For a one-off, or a source without a clean API, skip the script entirely: `curl GET /influencers`,
fetch the content yourself, then `curl POST /signals` for each item. The API is the whole
interface. The script is just the repeatable version of that same conversation.
