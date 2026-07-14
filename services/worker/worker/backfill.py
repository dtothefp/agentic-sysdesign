"""One-shot embedding backfill, for the moment you enable EMBEDDING_MODEL on a database that
already has signals.

Why this exists as its own thing, separate from the beat sweep (tasks.sweep_unembedded): the
sweep is a *production cost guard*, it only ever embeds source='instagram' rows, because silently
embedding thousands of seeded or demo signals is spend, not a safety net. But there are two times
you legitimately want to embed EVERYTHING captioned, sweep's filter and all:

  * local dev, where the only content you have is source='seed'/'demo' and you just want semantic
    search to light up so you can see it work, and
  * a first turn-on over an existing corpus, before the every-15-min sweep has had time to drain
    the instagram backlog.

So this is a manual, synchronous, source-agnostic backfill. Synchronous on purpose: no Celery, no
Redis, no worker process needed, just a DB connection and the embedding provider, so it runs the
same from a laptop, a devcontainer shell, or a Railway one-off. It reuses the exact embed_text +
insert_embedding primitives the sweep and the rating cache use, so it stores identical vectors,
insert is ON CONFLICT DO NOTHING so re-running is safe and only fills gaps.

    moon run worker:embed-backfill                 # embed all captioned, un-embedded signals
    uv run --package sysdesign-worker python -m worker.backfill --dry-run   # just count them
    uv run --package sysdesign-worker python -m worker.backfill --source instagram --limit 500
"""

from __future__ import annotations

import argparse
import sys

import psycopg
from common.db import DATABASE_URL
from common.embedding import EmbeddingError, default_embedding_model, embed_text, insert_embedding


def _select_unembedded(conn: psycopg.Connection, source: str | None, limit: int | None) -> list[tuple[str, str]]:
    """(content_hash, caption) for distinct captioned signals with no embedding yet. Same
    DISTINCT ON (content_hash) newest-wins collapse the sweep and the search hydrate use, so one
    piece of content is embedded once no matter how many rows carry it. Optional source filter and
    row cap; no cap (limit None/0) means the whole backlog."""
    sql = [
        "SELECT DISTINCT ON (r.content_hash) r.content_hash, r.payload->>'caption' AS caption",
        "FROM raw_signals r",
        "WHERE coalesce(r.payload->>'caption','') <> ''",
        "  AND NOT EXISTS (SELECT 1 FROM signal_embeddings se WHERE se.content_hash = r.content_hash)",
    ]
    params: list[object] = []
    if source:
        sql.append("  AND r.payload->>'source' = %s")
        params.append(source)
    sql.append("ORDER BY r.content_hash, r.captured_at DESC")
    if limit:
        sql.append("LIMIT %s")
        params.append(limit)
    return conn.execute("\n".join(sql), params).fetchall()


def backfill(source: str | None = None, limit: int | None = None, dry_run: bool = False, dsn: str | None = None) -> int:
    """Embed every captioned, un-embedded signal (optionally one source, optionally capped).
    Returns the number of embeddings written. dry_run reports the count and writes nothing."""
    model = default_embedding_model()
    if not model:
        print("EMBEDDING_MODEL is not set, nothing to do (search stays lexical-only).")
        return 0
    print(f"embedding model: {model}")

    with psycopg.connect(dsn or DATABASE_URL) as conn:
        rows = _select_unembedded(conn, source, limit)
        print(f"captioned signals needing an embedding: {len(rows)}" + (f" (source={source})" if source else ""))
        if dry_run or not rows:
            return 0

        written = failed = 0
        for i, (content_hash, caption) in enumerate(rows, 1):
            try:
                vector = embed_text(caption, model)
            except EmbeddingError as e:
                failed += 1
                # First failure is almost always the provider (no key, no quota, wrong dim). Stop
                # loudly rather than grinding through hundreds of identical failures.
                print(f"  embed failed on {content_hash[:12]}…: {e}", file=sys.stderr)
                if failed == 1:
                    print(
                        "  aborting on first embed failure (fix the provider, then re-run, it resumes where it stopped).",
                        file=sys.stderr,
                    )
                    break
                continue
            if insert_embedding(conn, content_hash, model, vector):
                written += 1
            if i % 50 == 0:
                conn.commit()
                print(f"  {i}/{len(rows)} processed, {written} written")
        conn.commit()
        print(f"done: {written} embeddings written, {failed} failed, {len(rows)} candidates.")
        return written


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill embeddings for captioned, un-embedded signals.")
    ap.add_argument("--source", help="only this payload source (e.g. instagram, demo, seed); default all captioned")
    ap.add_argument("--limit", type=int, default=0, help="max signals to embed (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="count candidates, write nothing")
    ap.add_argument("--dsn", help="override DATABASE_URL (e.g. a host connection into the container db)")
    args = ap.parse_args()
    backfill(source=args.source, limit=args.limit or None, dry_run=args.dry_run, dsn=args.dsn)


if __name__ == "__main__":
    main()
