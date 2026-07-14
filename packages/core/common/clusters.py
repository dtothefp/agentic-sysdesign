"""Module 6 (discovery half): group the week's rated posts into emergent themes.

The digest agent (Module 5) discovers themes bottom-up: it reads the week's rated posts and
groups them. Today the model does that grouping in-context, which caps how many posts it can
handle, when the week's posts no longer fit the context window (or fit but bury the signal in
noise), the model can't cluster what it can't hold. This layer moves the mechanical grouping OUT
of the model and into the pipeline, using the embeddings Module 6 already computes for search.

The split is the Module 5 thesis made literal: pipeline for volume, agent for judgment. Grouping
N posts by similarity is mechanical and O(n^2), that's the pipeline. Naming a theme and deciding
it matters is judgment, that's the agent. So get_signal_clusters returns pre-grouped themes (a
representative post, a size, an average relevance, the union of topics, and the member list for
drill-down) and the model reasons over ~15 themes instead of hundreds of raw posts. That scales
with volume, because the number of themes grows far slower than the number of posts.

Clusters are NOT predefined. There is no fixed taxonomy. Each call groups THIS week's posts by
how close their embeddings sit, so the themes are emergent from the data, a week where everyone
posts about voice agents produces a big voice-agents cluster on its own. The distance threshold is
the only knob, it sets how tight a group must be to count as one theme.

Two design choices worth defending in an interview:

  * Greedy threshold clustering, not k-means. We seed on the highest-relevance unassigned post,
    absorb everything within THRESHOLD cosine distance, and repeat. No k to pick up front, no new
    dependency (numpy/sklearn), and it reuses the same cosine notion as search and the rating
    cache. The trade is that it's order-sensitive and not globally optimal, fine for a legible
    weekly grouping, and the seed-on-relevance rule makes the representative the strongest post.
  * EXACT distances, not the HNSW index. HNSW is approximate, the right trade for search over the
    whole corpus. Clustering runs over the small weekly working set (rated + embedded posts), so
    we pull those vectors and compute exact cosine in Python. Correct grouping beats index speed
    when the set is already small.

Like search.py, the math is split out pure and unit-testable: cosine_distance and greedy_cluster
take vectors and ids, no database. get_signal_clusters wires them to Postgres. When no embeddings
back the window (EMBEDDING_MODEL unset in prod, the inert default), it returns clustered=False and
no themes, and the agent falls back to the flat get_rated_signals list.
"""

from __future__ import annotations

import math

import psycopg
from psycopg.rows import dict_row

from common.db import DATABASE_URL
from common.embedding import from_vector_literal

# How close (cosine distance) two posts must sit to land in the same theme. Looser than the rating
# cache's 0.05 (which means "near-duplicate content"), because a THEME is broader than a duplicate,
# several distinct posts about the same topic should merge. 0.35 is a sane starting point (roughly
# 0.65 cosine similarity); tune it against real weekly data. Overridable per call.
CLUSTER_THRESHOLD = 0.35


def cosine_distance(a: list[float], b: list[float]) -> float:
    """1 - cosine similarity, the same distance pgvector's <=> operator computes. 0.0 = identical
    direction, 1.0 = orthogonal, 2.0 = opposite. Pure, so the clustering logic is testable without
    a database or pgvector. We normalize here rather than assume unit vectors, the adapter doesn't
    guarantee the provider returned normalized embeddings."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 1.0
    return 1.0 - dot / (na * nb)


def greedy_cluster(items: list[dict], threshold: float = CLUSTER_THRESHOLD) -> list[list[dict]]:
    """Group items by embedding proximity, greedily. Each item is a dict carrying at least a
    'vector' (list[float]), a 'relevance' (float, the seed-ordering key), and a 'content_hash'
    (the deterministic tiebreak). Returns a list of clusters, each a list of the input dicts.

    Algorithm: order items by relevance (desc), then for each still-unassigned item make it a
    seed, absorb every other unassigned item within `threshold` cosine distance, and continue.
    Seeding on relevance means a cluster forms around its strongest post, which becomes the natural
    representative. Deterministic: the (relevance desc, content_hash) sort fixes seed order and
    tiebreaks, so the same input always yields the same clusters (stable across runs)."""
    ordered = sorted(items, key=lambda it: (-it["relevance"], it["content_hash"]))
    assigned: set[str] = set()
    clusters: list[list[dict]] = []
    for seed in ordered:
        if seed["content_hash"] in assigned:
            continue
        assigned.add(seed["content_hash"])
        cluster = [seed]
        for other in ordered:
            if other["content_hash"] in assigned:
                continue
            if cosine_distance(seed["vector"], other["vector"]) <= threshold:
                assigned.add(other["content_hash"])
                cluster.append(other)
        clusters.append(cluster)
    return clusters


def _theme(cluster: list[dict]) -> dict:
    """Shape one cluster (list of member rows, strongest first) into a theme the agent reasons
    over. The representative is the highest-relevance member (cluster[0] by construction). topics
    is the deduped union across members. members carries the full list so the agent can drill in
    (handle, url, content_hash) without a second search."""
    rep = cluster[0]
    topics: list[str] = []
    seen: set[str] = set()
    for m in cluster:
        for t in m.get("topics") or []:
            if t not in seen:
                seen.add(t)
                topics.append(t)
    avg_relevance = round(sum(m["relevance"] for m in cluster) / len(cluster), 3)
    return {
        "theme_size": len(cluster),
        "avg_relevance": avg_relevance,
        "topics": topics,
        "representative": {
            "handle": rep.get("handle"),
            "url": rep.get("url"),
            "caption": rep.get("caption"),
            "content_hash": rep["content_hash"],
            "relevance": rep["relevance"],
        },
        "members": [
            {
                "handle": m.get("handle"),
                "url": m.get("url"),
                "content_hash": m["content_hash"],
                "relevance": m["relevance"],
            }
            for m in cluster
        ],
    }


_SQL = """
    SELECT s.payload->>'handle' AS handle,
           s.payload->>'url' AS url,
           left(s.payload->>'caption', 200) AS caption,
           r.relevance, r.topics, r.content_hash,
           e.embedding::text AS embedding
    FROM signal_ratings r
    JOIN raw_signals s ON s.content_hash = r.content_hash
    JOIN signal_embeddings e ON e.content_hash = r.content_hash
    WHERE r.rated_at >= now() - make_interval(days => %s)
      AND r.relevance >= %s
"""

# How many rated posts sit in the window at all, regardless of whether they have embeddings. The
# gap between this and the clustered count is the embedding-coverage story the agent should see:
# rated posts with no embedding can't be clustered, so a large gap means "turn on EMBEDDING_MODEL".
_RATED_COUNT_SQL = """
    SELECT count(*) FROM signal_ratings
    WHERE rated_at >= now() - make_interval(days => %s) AND relevance >= %s
"""


def get_signal_clusters(
    days: int = 7,
    min_relevance: float = 0.5,
    max_themes: int = 15,
    threshold: float = CLUSTER_THRESHOLD,
    dsn: str | None = None,
) -> dict:
    """Emergent themes for the week, computed on demand from the rated+embedded posts.

    Returns a dict the digest agent (and GET /signal-clusters) consumes:
      rated_in_window   how many rated posts match the window/relevance filter
      embedded          how many of those had an embedding to cluster on
      clustered         False when embedded == 0 (no embeddings, fall back to get_rated_signals)
      theme_count       number of themes returned (<= max_themes)
      themes            each theme: size, avg_relevance, topics, representative, members
    Themes are ordered by size then avg_relevance (biggest, strongest first), then representative
    hash for determinism, so the most prominent theme leads. dsn overrides the target, same as
    get_rated_signals (the laptop runner points at Supabase, whose default it is not)."""
    with psycopg.connect(dsn or DATABASE_URL) as conn:
        rated_in_window = conn.execute(_RATED_COUNT_SQL, (days, min_relevance)).fetchone()[0]
        rows = conn.cursor(row_factory=dict_row).execute(_SQL, (days, min_relevance)).fetchall()

    if not rows:
        # Either nothing rated in the window, or nothing rated had an embedding. Either way there
        # is nothing to cluster; clustered=False tells the agent to fall back to the flat list.
        return {
            "days": days,
            "min_relevance": min_relevance,
            "rated_in_window": rated_in_window,
            "embedded": 0,
            "clustered": False,
            "theme_count": 0,
            "themes": [],
        }

    for row in rows:
        row["vector"] = from_vector_literal(row.pop("embedding"))

    clusters = greedy_cluster(rows, threshold=threshold)
    # Order by prominence: bigger themes first, then stronger average relevance, then the
    # representative hash so ties are deterministic (stable ordering across runs).
    clusters.sort(
        key=lambda c: (
            -len(c),
            -sum(m["relevance"] for m in c) / len(c),
            c[0]["content_hash"],
        )
    )
    themes = [_theme(c) for c in clusters[:max_themes]]
    return {
        "days": days,
        "min_relevance": min_relevance,
        "rated_in_window": rated_in_window,
        "embedded": len(rows),
        "clustered": True,
        "theme_count": len(themes),
        "themes": themes,
    }
