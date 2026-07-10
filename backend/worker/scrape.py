"""The actual per-influencer work a fan-out task does, in two modes.

live: the real Instagram scrape via Apify's run-sync-get-dataset-items endpoint, the same
      actor and payload shape the scrape-signals skill uses, but writing straight to Postgres
      through common.signals.insert_signal instead of POSTing over HTTP (the worker is a
      writer in its own right now, not an API client).

demo: synthetic signals with a small sleep between each, so you can watch the fan-out and the
      SSE progress bar move without spending Apify credits. Same idempotent write path, so it
      exercises the real insert and the real runs-table accounting.

Both return (inserted_count, new_items) where new_items is one {content_hash, caption} per
signal this call actually inserted (ON CONFLICT misses excluded). The count feeds the run's
running total; the items are what the task enqueues rate_signal jobs for, because "which
rows are new" is knowledge only the writer has at write time (Module 4's rating layer keys
off exactly this).
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
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # Apify puts the actual reason (e.g. which input field failed validation) in the
        # response body; without this, all the run's error field shows is "400 Bad Request".
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Apify {e.code}: {detail}") from None


def scrape_influencer_live(
    conn: psycopg.Connection, inf: dict, run_ts: str, limit: int
) -> tuple[int, list[dict]]:
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
        # Apify validates this field against a regex that only accepts ISO timestamps
        # ending in Z, not +00:00, which is what Python's isoformat() emits for UTC.
        wm = watermark if isinstance(watermark, datetime) else datetime.fromisoformat(str(watermark))
        actor_input["onlyPostsNewerThan"] = wm.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    posts = _apify_run(load_apify_key(), actor_input)

    inserted = 0
    new_items: list[dict] = []
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
            did, h = insert_signal(conn, inf["id"], ts, payload)
            if did:
                inserted += 1
                new_items.append({"content_hash": h, "caption": payload["caption"]})
        except psycopg.errors.CheckViolation:
            # no partition covers this post's month; skip it (same rule the API enforces)
            conn.rollback()
    conn.commit()

    # advance the watermark so the next run only pulls newer posts
    conn.execute(
        "UPDATE influencers SET last_scraped_at = %s WHERE id = %s", (run_ts, inf["id"])
    )
    conn.commit()
    return inserted, new_items


def scrape_influencer_demo(
    conn: psycopg.Connection, inf: dict, run_ts: str, limit: int
) -> tuple[int, list[dict]]:
    """Insert `limit` synthetic signals for this influencer, one every ~0.4s so the progress
    stream visibly ticks. No Apify call. Each payload is distinct (carries an index) so the
    content_hash differs and the ON CONFLICT upsert actually inserts rather than deduping."""
    handle = inf["instagram_handle"]
    now = datetime.now(timezone.utc)
    inserted = 0
    new_items: list[dict] = []
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
            did, h = insert_signal(conn, inf["id"], captured_at, payload)
            if did:
                inserted += 1
                new_items.append({"content_hash": h, "caption": payload["caption"]})
            conn.commit()
        except psycopg.errors.CheckViolation:
            conn.rollback()  # no partition for the current month; nothing to insert
            break
    return inserted, new_items
