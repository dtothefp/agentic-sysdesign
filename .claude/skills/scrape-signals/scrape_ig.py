"""Scrape each watchlist influencer's recent Instagram posts (via the Apify REST API) and
POST them to the sysdesign API as raw_signals, all influencers in parallel.

Incremental by design, driven by each influencer's last_scraped_at watermark:
  * First run (last_scraped_at is NULL): pull only the single most recent post.
  * Later runs: pull posts published after last_scraped_at.
After a run, the influencer's watermark is advanced to the run start time, so the next run
only sees genuinely new posts.

Idempotency: the signal payload is the stable identity of a post (shortcode, url, type,
caption, posted_at), NOT volatile metrics or expiring CDN urls. The API derives content_hash
from that payload, so re-scraping the same post is an ON CONFLICT no-op. Engagement-over-time
(likes/comments changing) would be a separate observations table, a good follow-up.

This is the scrape-signals skill's repeatable path, run by Claude Code (not a Make target).
It talks to the running API over HTTP the same way any client would: GET /influencers to learn
who to scrape, POST /signals for each post, PATCH /influencers/{id} to advance the watermark.

Apify: the REST run-sync-get-dataset-items endpoint runs the actor and returns its dataset
in one blocking call, no MCP, no SDK. APIFY_API_KEY is read from the environment or the repo-root .env.

Usage (from the repo root, with the API running via `moon run api:dev`):
  uv run python .claude/skills/scrape-signals/scrape_ig.py
  uv run python .claude/skills/scrape-signals/scrape_ig.py --handle nick_saraev --limit 30
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

APIFY_ACTOR = "apify~instagram-scraper"  # ~ is the REST form of the apify/instagram-scraper slug


def load_apify_key() -> str:
    """Env first, then the repo-root .env (walking up from this file, anchored on the .moon
    dir so we never read a parent workspace's .env). Never hard-code the key."""
    key = os.environ.get("APIFY_API_KEY")
    if key:
        return key
    for base in [Path.cwd(), *Path(__file__).resolve().parents]:
        env = base / ".env"
        if (base / ".moon").is_dir() and env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("APIFY_API_KEY="):
                    return line.split("=", 1)[1].strip()
    sys.exit("APIFY_API_KEY not set. Export it or put it in the repo-root .env")


def http_json(url: str, method: str = "GET", body: dict | None = None, timeout: int = 300):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def apify_run(key: str, actor_input: dict) -> list[dict]:
    """Run the actor synchronously and get its dataset items back in one call."""
    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items?token={key}"
    return http_json(url, method="POST", body=actor_input)


def scrape_one(api: str, key: str, inf: dict, run_ts: str, limit: int, backfill: bool = False) -> str:
    """Scrape one influencer end to end: fetch, post each signal, advance the watermark.

    Two modes. Normal (incremental) looks forward off the watermark. Backfill looks backward:
    it pulls the last `limit` posts regardless of the watermark, to fill older partitions, and
    deliberately does NOT touch the watermark (backfill is orthogonal to the forward cursor)."""
    handle = inf["instagram_handle"]
    watermark = inf.get("last_scraped_at")
    actor_input = {
        "directUrls": [f"https://www.instagram.com/{handle}/"],
        "resultsType": "posts",
    }
    if backfill:
        # look backward: the last `limit` posts, watermark ignored.
        actor_input["resultsLimit"] = limit
    else:
        # first run: just the most recent post. incremental run: up to `limit` newer posts.
        actor_input["resultsLimit"] = 1 if watermark is None else limit
        if watermark is not None:
            actor_input["onlyPostsNewerThan"] = watermark

    try:
        posts = apify_run(key, actor_input)
    except urllib.error.HTTPError as e:
        return f"  {handle:16} APIFY ERROR {e.code}: {e.read().decode()[:120]}"
    except urllib.error.URLError as e:
        return f"  {handle:16} APIFY ERROR: {e}"

    inserted = duped = skipped = 0
    for p in posts:
        ts = p.get("timestamp")
        if not ts:
            skipped += 1
            continue
        payload = {
            "source": "instagram",
            "handle": handle,
            "shortcode": p.get("shortCode"),
            "url": p.get("url"),
            "type": p.get("type"),  # 'Image' | 'Video' (reel) | 'Sidecar'
            "caption": p.get("caption"),
            "posted_at": ts,
        }
        try:
            res = http_json(
                f"{api}/signals",
                method="POST",
                body={"influencer_id": inf["id"], "captured_at": ts, "payload": payload},
                timeout=30,
            )
            inserted += 1 if res.get("inserted") else 0
            duped += 0 if res.get("inserted") else 1
        except urllib.error.HTTPError as e:
            # 400 = no partition covers the post's month (provision it) or unknown influencer.
            skipped += 1
            if e.code != 400:
                return f"  {handle:16} SIGNAL ERROR {e.code}: {e.read().decode()[:120]}"

    # advance the watermark to the run start, so the next run only pulls newer posts. Backfill
    # skips this: it's filling the past, not moving the forward cursor.
    if not backfill:
        try:
            http_json(f"{api}/influencers/{inf['id']}", method="PATCH", body={"last_scraped_at": run_ts}, timeout=30)
        except urllib.error.HTTPError as e:
            return f"  {handle:16} WATERMARK ERROR {e.code}"

    if backfill:
        mode = " (backfill: last N, no partition = skipped)"
    elif watermark is None:
        mode = " (first run: newest only)"
    else:
        mode = ""
    return f"  {handle:16} posts:{len(posts):3}  inserted:{inserted:3}  already-had:{duped:3}  skipped:{skipped:3}{mode}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--handle", help="scrape only this handle (default: every influencer)")
    ap.add_argument("--limit", type=int, default=50, help="max posts per incremental run")
    ap.add_argument(
        "--backfill",
        action="store_true",
        help="pull the last --limit posts per influencer regardless of the watermark, "
        "to fill older partitions. Does not advance the watermark.",
    )
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    key = load_apify_key()
    influencers = http_json(f"{args.api}/influencers")
    if args.handle:
        influencers = [i for i in influencers if i["instagram_handle"] == args.handle.lstrip("@")]
        if not influencers:
            sys.exit(f"no influencer with handle {args.handle}. Seed it or POST /influencers first.")

    # one run timestamp for the whole batch; each influencer's watermark advances to it.
    run_ts = datetime.now(UTC).isoformat()
    mode = f"backfill last {args.limit}" if args.backfill else "incremental"
    print(f"scraping {len(influencers)} influencer(s) in parallel ({mode})...")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(scrape_one, args.api, key, inf, run_ts, args.limit, args.backfill) for inf in influencers]
        for f in as_completed(futures):
            print(f.result())


if __name__ == "__main__":
    main()
