"""Seed the influencer watchlist plus a few thousand synthetic raw_signals spread across
three months so partition pruning has something to prune.

Two distinct things get seeded, on purpose:
  * The real Defrag watchlist (name + Instagram handle). These are the rows the scraper
    fills with actual posts. Re-adding a handle is a no-op (ON CONFLICT on the handle).
  * Synthetic signal volume, so the EXPLAIN drills mean something before any scraping. A
    handful of real posts per creator wouldn't move a query plan; 4000 rows across three
    partitions will. These are generic filler, not real content.

Every insert is idempotent on the locked unique key, so running this twice inserts nothing
the second time. That one line of SQL is the idempotency contract and the answer to most
"what if it runs twice" probes.

Run:  uv run python -m common.seed
"""
import json
import random
from datetime import datetime, timedelta, timezone

import psycopg

from common.db import DATABASE_URL
from common.hashing import content_hash

# The Defrag influencer watchlist: (display name, instagram handle). Handles verified against
# the creator profiles in package-defrag/research/bots/ (all five are verified IG accounts).
# Edit here or POST /influencers to adjust.
WATCHLIST = [
    ("Lewis Menelaws", "lewismenelaws"),
    ("Nick Saraev", "nick_saraev"),
    ("Angus Sewell McCann", "angus.sewell"),
    ("Rourke", "rourke"),
    ("RPN", "rpn"),
]
# deterministic so re-seeding produces the same content_hash set (true idempotency)
random.seed(42)


def main() -> None:
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        ids = []
        for name, handle in WATCHLIST:
            cur.execute(
                "INSERT INTO influencers (name, instagram_handle) VALUES (%s, %s) "
                "ON CONFLICT (instagram_handle) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id",
                (name, handle),
            )
            ids.append(cur.fetchone()[0])

        start = datetime(2026, 5, 1, tzinfo=timezone.utc)
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
