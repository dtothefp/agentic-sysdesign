"""Seed the influencer watchlist plus a few thousand synthetic raw_signals spread across
three months so partition pruning has something to prune.

Two distinct things get seeded, on purpose:
  * The Defrag watchlist (name + Instagram handle), read from the scrape-signals skill's
    watchlist.json so there's one source of truth. These are the rows the scraper fills with
    real posts. Re-adding a handle is a no-op (ON CONFLICT on the handle).
  * Synthetic signal volume, so the EXPLAIN drills mean something before any scraping. A
    handful of real posts per creator wouldn't move a query plan; 4000 rows across three
    partitions will. These are generic filler, not real content.

This is the one-shot convenience so `make db-init && make drills` works. The skill does the
same watchlist load through the API (POST /influencers/bulk) when Claude Code drives it live.
Both read the same watchlist.json, so they can't drift.

Every insert is idempotent on the locked unique key, so running this twice inserts nothing
the second time. That one line of SQL is the idempotency contract and the answer to most
"what if it runs twice" probes.

Run:  uv run python -m common.seed                    # influencers + 4000 synthetic signals
      uv run python -m common.seed --influencers-only  # just the watchlist, no signals
"""

import argparse
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

from common.db import DATABASE_URL
from common.hashing import content_hash

# Single source of truth for who we track: the skill's watchlist.json. Fallback keeps the
# seed runnable if the skill dir isn't present (e.g. a Python-only checkout).
_WATCHLIST_JSON = Path(__file__).resolve().parents[2] / ".claude" / "skills" / "scrape-signals" / "watchlist.json"
_FALLBACK = [{"name": "Example Creator", "instagram_handle": "example_creator"}]


def load_watchlist() -> list[dict]:
    if _WATCHLIST_JSON.exists():
        return json.loads(_WATCHLIST_JSON.read_text())
    return _FALLBACK


# deterministic so re-seeding produces the same content_hash set (true idempotency)
random.seed(42)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--influencers-only",
        action="store_true",
        help="seed just the watchlist, skip the 4000 synthetic signals (drill volume)",
    )
    args = ap.parse_args()

    watchlist = load_watchlist()
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        ids = []
        for row in watchlist:
            cur.execute(
                "INSERT INTO influencers (name, instagram_handle) VALUES (%s, %s) "
                "ON CONFLICT (instagram_handle) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id",
                (row["name"], row["instagram_handle"].lstrip("@").lower()),
            )
            ids.append(cur.fetchone()[0])

        if args.influencers_only:
            conn.commit()
            print(f"influencers: {len(ids)} (no signals seeded)")
            return

        start = datetime(2026, 5, 1, tzinfo=UTC)
        inserted = 0
        for i in range(4000):
            influencer_id = random.choice(ids)
            captured = start + timedelta(minutes=random.randint(0, 60 * 24 * 90))
            payload = {"source": "seed", "seq": i, "text": f"signal {random.random()}"}
            cur.execute(
                "INSERT INTO raw_signals "
                "(influencer_id, source_id, captured_at, content_hash, payload) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (influencer_id, content_hash, captured_at) DO NOTHING",
                (influencer_id, None, captured, content_hash(payload), json.dumps(payload)),
            )
            inserted += cur.rowcount
        conn.commit()
        print(f"influencers: {len(ids)}  raw_signals inserted this run: {inserted}")


if __name__ == "__main__":
    main()
