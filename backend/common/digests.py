"""Module 5: the enriched-rows query behind the digest agent's get_rated_signals tool.

This is the join GET /ratings deliberately doesn't offer: ratings attached back to
their source posts (handle, URL, caption). It lives in common/ because two callers
share it, the Celery worker answering the agent's custom tool call (worker/tasks.py)
and the laptop demo runner (m5_agents/run_digest.py). When Module 5 graduates the
custom tool to a plain GET /rated-signals endpoint, the API becomes the third caller
and the custom-tool plumbing goes away; this function outlives all of those doorways.

No FK backs the join. content_hash is the by-convention key between signal_ratings
and the partitioned raw_signals (same reason as in the Module 4 migration notes).
"""
import psycopg
from psycopg.rows import dict_row

from common.db import DATABASE_URL

_SQL = """
    SELECT s.payload->>'handle' AS handle,
           s.payload->>'url' AS url,
           left(s.payload->>'caption', 200) AS caption,
           s.captured_at,
           r.relevance, r.confidence, r.topics, r.summary
    FROM signal_ratings r
    JOIN raw_signals s ON s.content_hash = r.content_hash
    WHERE r.rated_at >= now() - make_interval(days => %s)
      AND r.relevance >= %s
    ORDER BY r.relevance DESC, r.rated_at DESC
    LIMIT 100
"""


def get_rated_signals(
    days: int = 7, min_relevance: float = 0.5, dsn: str | None = None
) -> list[dict]:
    """Rated posts joined to their source signals, best first, capped at 100.

    dsn overrides the connection target; the default (DATABASE_URL) is right wherever
    this runs next to its own database (the Railway worker, the dev container). The
    laptop runner passes DATABASE_URL_SUPABASE explicitly because its local default
    points at the drill database, which has no real ratings."""
    with psycopg.connect(dsn or DATABASE_URL) as conn:
        return (
            conn.cursor(row_factory=dict_row)
            .execute(_SQL, (days, min_relevance))
            .fetchall()
        )
