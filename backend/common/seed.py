"""Load 5 competitors and a few thousand raw_signals spread across three months so
partition pruning has something to prune. Every insert is idempotent on the locked
unique key, so running this twice inserts nothing the second time. That one line of
SQL is the idempotency contract and the answer to most "what if it runs twice" probes.

Run:  uv run python -m common.seed
"""
import hashlib
import json
import random
from datetime import datetime, timedelta, timezone

import psycopg

from common.db import DATABASE_URL

COMPETITORS = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
# deterministic so re-seeding produces the same content_hash set (true idempotency)
random.seed(42)


def content_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def main() -> None:
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        ids = {}
        for name in COMPETITORS:
            cur.execute(
                "INSERT INTO competitors (name) VALUES (%s) "
                "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
                (name,),
            )
            ids[name] = cur.fetchone()[0]

        start = datetime(2026, 5, 1, tzinfo=timezone.utc)
        inserted = 0
        for i in range(4000):
            name = random.choice(COMPETITORS)
            captured = start + timedelta(minutes=random.randint(0, 60 * 24 * 90))
            payload = {"competitor": name, "seq": i, "text": f"signal {random.random()}"}
            cur.execute(
                "INSERT INTO raw_signals "
                "(competitor_id, source_id, captured_at, content_hash, payload) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (competitor_id, content_hash, captured_at) DO NOTHING",
                (ids[name], None, captured, content_hash(payload), json.dumps(payload)),
            )
            inserted += cur.rowcount
        conn.commit()
        print(f"competitors: {len(ids)}  raw_signals inserted this run: {inserted}")


if __name__ == "__main__":
    main()
