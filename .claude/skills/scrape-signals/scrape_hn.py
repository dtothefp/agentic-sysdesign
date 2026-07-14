"""Scrape Hacker News (Algolia API, no auth, stdlib only) for mentions of a competitor and
POST each story to the sysdesign API as a raw_signal.

captured_at is the story's own publish time, so re-running is idempotent: the same story
hashes to the same content_hash at the same captured_at, and the API's ON CONFLICT DO
NOTHING makes the second run a no-op. Stories whose publish time predates the existing
monthly partitions are skipped (the API returns 400), which is the partitioning lesson in
practice: no partition, no insert.

Secondary example only. The primary scraper is scrape_ig.py (the Defrag watchlist). This one
stays as a template for any source that exposes clean JSON, mapped onto the same /signals path.

Usage (from the repo root, with the API running via `make api`):
  uv run python .claude/skills/scrape-signals/scrape_hn.py \
      --influencer-id 1 --query "Postgres" --limit 20
"""

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request


def fetch_hn(query: str, limit: int) -> list[dict]:
    url = "https://hn.algolia.com/api/v1/search_by_date?" + urllib.parse.urlencode(
        {"query": query, "tags": "story", "hitsPerPage": limit}
    )
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)["hits"]


def post_signal(api: str, influencer_id: int, captured_at: str, payload: dict) -> dict:
    body = json.dumps({"influencer_id": influencer_id, "captured_at": captured_at, "payload": payload}).encode()
    req = urllib.request.Request(
        api + "/signals",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--influencer-id", type=int, required=True)
    ap.add_argument("--query", required=True, help="HN search term (e.g. a competitor name)")
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    hits = fetch_hn(args.query, args.limit)
    inserted = duped = skipped = 0
    for h in hits:
        # HN gives "2026-07-08T12:00:00.000Z"; normalize the Z so fromisoformat accepts it.
        captured_at = (h.get("created_at") or "").replace("Z", "+00:00")
        if not captured_at:
            skipped += 1
            continue
        payload = {
            "source": "hackernews",
            "title": h.get("title"),
            "url": h.get("url"),
            "author": h.get("author"),
            "points": h.get("points"),
            "num_comments": h.get("num_comments"),
            "hn_object_id": h.get("objectID"),
            "story_time": h.get("created_at"),
        }
        try:
            res = post_signal(args.api, args.influencer_id, captured_at, payload)
            if res.get("inserted"):
                inserted += 1
            else:
                duped += 1
        except urllib.error.HTTPError as e:
            # 400 = no partition for that publish time (or unknown competitor_id). Skip it.
            skipped += 1
            if e.code != 400:
                print(f"  ! {e.code} on {payload['hn_object_id']}: {e.read().decode()[:200]}")

    print(f"hn hits: {len(hits)}  inserted: {inserted}  already-had: {duped}  skipped: {skipped}")


if __name__ == "__main__":
    main()
