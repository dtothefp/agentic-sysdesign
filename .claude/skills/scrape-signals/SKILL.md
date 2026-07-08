---
name: scrape-signals
description: Populate the sysdesign raw_signals table with real competitor data by scraping public sources (Hacker News to start) and POSTing them through the FastAPI backend. Use when you want real data in the database to run the EXPLAIN drills against, instead of the synthetic seed. Trigger phrases include "scrape signals for X", "populate the db with real data", "add real competitor signals".
---

# scrape-signals

Fills `raw_signals` with real, messy, public data by going through the API, not by writing
to Postgres directly. That's the point: every insert takes the same idempotent
`ON CONFLICT DO NOTHING` path the seed and the app use, so the data-integrity story stays
honest.

This is Module 1's Phase B. It turns the synthetic seed into a real firehose so the drills
mean something.

## Prerequisites

1. The db is up and migrated. From `backend/`: `make setup` (host) or `make db-init` (dev container).
2. The API is running. From `backend/`: `make api`. Confirm with `curl -s localhost:8000/health`.
3. The competitor exists. Create it and note its `id`:
   ```bash
   curl -s -X POST localhost:8000/competitors \
     -H 'Content-Type: application/json' \
     -d '{"name": "Supabase", "domain": "supabase.com"}'
   ```

## Run the included Hacker News scraper

`scrape_hn.py` (in this skill dir) hits the public HN Algolia API (no auth, stdlib only)
and POSTs each matching story as a signal. Run it from the repo root:

```bash
uv run --project backend python .claude/skills/scrape-signals/scrape_hn.py \
    --competitor-id <id> --query "<search term>" --limit 20
```

It prints `inserted / already-had / skipped`. Run it twice: the second run should report
0 inserted and everything as already-had. That's the idempotency contract working, and a
clean thing to show in an interview.

## The captured_at rule (read before adding sources)

`raw_signals` is RANGE partitioned by `captured_at`, and a row only inserts if a partition
covers its timestamp. The scraper uses each story's own publish time as `captured_at`, so:

- Recent items land in the current month's partition and insert fine.
- Items older than the oldest partition are rejected by the API (400) and counted as
  skipped. That's expected, not a bug. To ingest older data, provision the month first with
  a migration that calls `create_month_partition('<any date in that month>')`.

## Adding another source

Copy `scrape_hn.py` as a template. A new scraper needs to:

1. Fetch from a public source (RSS, a changelog JSON, a GitHub releases API, Reddit's
   `.json` endpoints).
2. Shape each item into a `payload` dict (put the source's own id, url, title, timestamp,
   and any metrics in there). Keep a stable `source` key so you can tell scrapers apart.
3. Pick `captured_at` (the item's publish time keeps re-runs idempotent; `now()` would make
   every run a fresh observation, which is also valid but grows the table each run).
4. POST to `/signals`. The API derives `content_hash` from the payload, so you never compute
   it client-side.

Optionally register the source first via `POST /sources` and pass its `id` in the signal so
you can trace provenance.

## Driving it with Claude Code directly

For sources without a clean API, you can skip the script: fetch the page yourself (WebFetch
or curl), extract the items, and POST each with a `curl` to `/signals`. The script is just
the repeatable path for sources that expose JSON.
