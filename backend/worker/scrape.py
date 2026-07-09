"""The actual per-influencer work a fan-out task does, in two modes.

live: the real Instagram scrape via Apify's run-sync-get-dataset-items endpoint, the same
      actor and payload shape the scrape-signals skill uses, but writing straight to Postgres
      through common.signals.insert_signal instead of POSTing over HTTP (the worker is a
      writer in its own right now, not an API client).

demo: synthetic signals with a small sleep between each, so you can watch the fan-out and the
      SSE progress bar move without spending Apify credits. Same idempotent write path, so it
      exercises the real insert and the real runs-table accounting.

Both return how many signals they actually inserted (ON CONFLICT misses excluded), which the
task adds to the run's running total.
"""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

from common.signals import insert_signal

APIFY_ACTOR = "apify~instagram-scraper"  # ~ is the REST form of the apify/instagram-scraper slug


def load_apify_key() -> str:
    """Env first, then backend/.env (walking up from this file). Never hard-code the key."""
    key = os.environ.get("APIFY_API_KEY")
    if key:
        return key
    for base in [Path.cwd(), *Path(__file__).resolve().parents]:
        env = base / "backend" / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("APIFY_API_KEY="):
                    return line.split("=", 1)[1].strip()
    raise RuntimeError("APIFY_API_KEY not set. Export it or put it in backend/.env")


def _apify_run(key: str, actor_input: dict) -> list[dict]:
    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items?token={key}"
    req = urllib.request.Request(
        url,
        data=json.dumps(actor_input).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def scrape_influencer_live(conn: psycopg.Connection, inf: dict, run_ts: str, limit: int) -> int:
    """Fetch this influencer's recent posts and upsert each as a signal. Incremental off the
    watermark (first run: newest post only; later runs: posts newer than last_scraped_at),
    then advance the watermark to the run start."""
    handle = inf["instagram_handle"]
    watermark = inf.get("last_scraped_at")
    actor_input = {
        "directUrls": [f"https://www.instagram.com/{handle}/"],
        "resultsType": "posts",
        "resultsLimit": 1 if watermark is None else limit,
    }
    if watermark is not None:
        actor_input["onlyPostsNewerThan"] = (
            watermark.isoformat() if isinstance(watermark, datetime) else str(watermark)
        )

    posts = _apify_run(load_apify_key(), actor_input)

    inserted = 0
    for p in posts:
        ts = p.get("timestamp")
        if not ts:
            continue
        payload = {
            "source": "instagram",
            "handle": handle,
            "shortcode": p.get("shortCode"),
            "url": p.get("url"),
            "type": p.get("type"),
            "caption": p.get("caption"),
            "posted_at": ts,
        }
        try:
            did, _ = insert_signal(conn, inf["id"], ts, payload)
            inserted += 1 if did else 0
        except psycopg.errors.CheckViolation:
            # no partition covers this post's month; skip it (same rule the API enforces)
            conn.rollback()
    conn.commit()

    # advance the watermark so the next run only pulls newer posts
    conn.execute(
        "UPDATE influencers SET last_scraped_at = %s WHERE id = %s", (run_ts, inf["id"])
    )
    conn.commit()
    return inserted


def scrape_influencer_demo(conn: psycopg.Connection, inf: dict, run_ts: str, limit: int) -> int:
    """Insert `limit` synthetic signals for this influencer, one every ~0.4s so the progress
    stream visibly ticks. No Apify call. Each payload is distinct (carries an index) so the
    content_hash differs and the ON CONFLICT upsert actually inserts rather than deduping."""
    handle = inf["instagram_handle"]
    now = datetime.now(timezone.utc)
    inserted = 0
    for i in range(limit):
        time.sleep(0.4)
        captured_at = now - timedelta(minutes=i)  # spread within the current-month partition
        payload = {
            "source": "demo",
            "handle": handle,
            "shortcode": f"demo-{inf['id']}-{run_ts}-{i}",
            "caption": f"synthetic signal {i} for {handle}",
            "posted_at": captured_at.isoformat(),
        }
        try:
            did, _ = insert_signal(conn, inf["id"], captured_at, payload)
            inserted += 1 if did else 0
            conn.commit()
        except psycopg.errors.CheckViolation:
            conn.rollback()  # no partition for the current month; nothing to insert
            break
    return inserted
