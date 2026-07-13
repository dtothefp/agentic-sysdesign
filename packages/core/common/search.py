"""Module 6: hybrid search over signal content. Two retrieval methods, one fused ranking.

The problem lexical search alone can't solve: a query for "autonomous agents" misses a post
titled "self-directed LLM workflows" (no shared words, same meaning). The problem semantic
search alone can't solve: a query for an exact handle, a product name, or a rare acronym that
was never common enough to sit near anything in embedding space. Real systems run BOTH and
fuse them, so each covers the other's blind spot.

  * LEXICAL  Postgres full-text search. The generated caption_tsv column (a tsvector) matched
    against websearch_to_tsquery, ranked by ts_rank_cd, served by the GIN inverted index.
    Exact and offline, no model. This half works the moment the migration lands.
  * SEMANTIC pgvector. The query is embedded with the same model the documents were, and the
    HNSW index returns approximate nearest neighbors by cosine distance. Meaning, not words.
    Inert until an embedding provider fills signal_embeddings (EMBEDDING_MODEL set); when off,
    hybrid_search runs lexical-only and still returns useful results.

Fusion is Reciprocal Rank Fusion (RRF). The two methods score on totally different, un-
comparable scales (a ts_rank around 0.0-1.0, a cosine distance around 0.0-2.0), so you can't
just add or average them. RRF throws the scores away and fuses by RANK POSITION alone: a
document's contribution from each list is 1/(k+rank), summed across lists. Rank 1 in a list is
worth 1/(k+1) no matter what raw score produced it, which is exactly what makes RRF robust to
mismatched scales and the reason it's the default fusion in Elasticsearch and OpenSearch.

This module is split so the fusion math is pure and unit-testable with no database:
reciprocal_rank_fusion takes ranked id lists and returns a fused order. The SQL helpers and
hybrid_search wire it to Postgres. Passing query_embedding=None keeps the semantic half out
entirely, which is both the EMBEDDING_MODEL-unset path and how tests exercise lexical-only.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from common.db import DATABASE_URL
from common.embedding import EmbeddingError, default_embedding_model, embed_text, to_vector_literal

# RRF's one constant. k dampens how much the very top ranks dominate: a large k flattens the
# 1/(k+rank) curve so ranks 1 and 2 are nearly equal, a small k makes rank 1 far outweigh
# everything below it. 60 is the value from the original RRF paper (Cormack et al. 2009) and
# the default Elasticsearch/OpenSearch ship, kept here so the behavior matches what an
# interviewer expects rather than a hand-tuned mystery number.
RRF_K = 60


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    """Fuse N ranked id lists into one, by rank position alone. Pure, no I/O.

    rankings: each inner list is one method's result, best-first (index 0 = its top hit). Ids
              may appear in several lists (a doc both methods found) or just one.
    returns:  (id, fused_score) pairs, highest score first. A doc's score is the sum over the
              lists it appears in of 1/(k + rank), where rank is its 1-based position in that
              list. Appearing high in both lists beats appearing high in only one, which is the
              whole point, agreement across methods is the signal.
    """
    scores: dict[str, float] = {}
    for ranked in rankings:
        for position, doc_id in enumerate(ranked):
            rank = position + 1  # 1-based: the top hit is rank 1, worth 1/(k+1)
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    # Sort by fused score desc. Ties broken by id for a deterministic order (matters for tests
    # and for stable pagination); the id tiebreak is arbitrary but reproducible.
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def _fts_candidates(conn: psycopg.Connection, query: str, limit: int) -> list[str]:
    """Lexical candidate ids, best-first. websearch_to_tsquery parses a user-style query
    ("agent orchestration", quoted phrases, OR, -negation) into a tsquery; ts_rank_cd ranks by
    lexeme frequency AND proximity (cd = cover density, it rewards matched terms appearing close
    together). The GIN index on caption_tsv serves the @@ match.

    DISTINCT ON collapses the same content_hash appearing under multiple influencers (the
    raw_signals dedup key is (influencer_id, content_hash, captured_at), so one piece of content
    can legitimately have several rows); we want one rank slot per distinct piece of content."""
    rows = conn.execute(
        """
        SELECT content_hash FROM (
            SELECT DISTINCT ON (s.content_hash)
                   s.content_hash,
                   ts_rank_cd(s.caption_tsv, q) AS score
            FROM raw_signals s, websearch_to_tsquery('english', %s) q
            WHERE s.caption_tsv @@ q
            ORDER BY s.content_hash, score DESC
        ) t
        ORDER BY score DESC
        LIMIT %s
        """,
        (query, limit),
    ).fetchall()
    return [r[0] for r in rows]


def _vector_candidates(conn: psycopg.Connection, query_embedding: list[float], limit: int) -> list[str]:
    """Semantic candidate ids, best-first (nearest cosine distance). The <=> operator is
    pgvector's cosine distance; ORDER BY <=> ... LIMIT is the exact shape the HNSW index
    accelerates (it walks its small-world graph instead of scanning every row). ef_search trades
    recall for latency; the default is fine at this corpus size, so we don't set it per-query.

    signal_embeddings has content_hash as its PK, so no DISTINCT ON is needed here."""
    literal = to_vector_literal(query_embedding)
    rows = conn.execute(
        "SELECT content_hash FROM signal_embeddings ORDER BY embedding <=> %s::vector LIMIT %s",
        (literal, limit),
    ).fetchall()
    return [r[0] for r in rows]


def _hydrate(conn: psycopg.Connection, hashes: list[str]) -> dict[str, dict]:
    """Fetch the display row for each winning hash in ONE query (no N+1), keyed by hash so the
    caller can re-order by fused score. LEFT JOIN signal_ratings so a hit carries its AI rating
    when Module 4 has rated it and nulls when it hasn't, search doesn't depend on rating. Same
    DISTINCT ON collapse as the lexical candidate query, newest row wins for a given hash."""
    if not hashes:
        return {}
    rows = (
        conn.cursor(row_factory=dict_row)
        .execute(
            """
            SELECT DISTINCT ON (s.content_hash)
                   s.content_hash,
                   s.payload->>'handle' AS handle,
                   s.payload->>'url' AS url,
                   left(s.payload->>'caption', 200) AS caption,
                   s.captured_at,
                   r.relevance, r.summary, r.topics
            FROM raw_signals s
            LEFT JOIN signal_ratings r ON r.content_hash = s.content_hash
            WHERE s.content_hash = ANY(%s)
            ORDER BY s.content_hash, s.captured_at DESC
            """,
            (hashes,),
        )
        .fetchall()
    )
    return {row["content_hash"]: row for row in rows}


def hybrid_search(
    conn: psycopg.Connection,
    query: str,
    query_embedding: list[float] | None,
    limit: int = 20,
    k: int = RRF_K,
    candidate_pool: int = 50,
) -> list[dict]:
    """Run both retrieval halves, fuse with RRF, hydrate the top `limit` into display rows.

    query_embedding None (EMBEDDING_MODEL unset, or the query embed failed) means lexical-only:
    RRF over a single list degrades to that list's own order, so the endpoint still works with
    no model. candidate_pool is how deep each method reports before fusion; it should exceed
    limit so a doc ranked, say, #30 by FTS but #1 by vectors can still surface.

    Each returned row carries its fused `score` and which methods found it (`sources`), so the
    caller/interviewer can SEE that a top hit was corroborated by both halves vs found by one.
    """
    fts = _fts_candidates(conn, query, candidate_pool)
    vec = _vector_candidates(conn, query_embedding, candidate_pool) if query_embedding is not None else []

    rankings = [lst for lst in (fts, vec) if lst]
    if not rankings:
        return []
    fused = reciprocal_rank_fusion(rankings, k)[:limit]

    fts_set, vec_set = set(fts), set(vec)
    rows = _hydrate(conn, [h for h, _ in fused])
    out: list[dict] = []
    for content_hash, score in fused:
        row = rows.get(content_hash)
        if row is None:  # hash was in an index but its raw_signals row vanished; skip defensively
            continue
        sources = [name for name, s in (("lexical", fts_set), ("semantic", vec_set)) if content_hash in s]
        out.append({**row, "score": round(score, 6), "sources": sources})
    return out


def embed_query(query: str) -> list[float] | None:
    """Embed a search query with the default embedding model, or return None when the semantic
    half shouldn't run: EMBEDDING_MODEL unset, or the embed call failed. Centralizing the
    degrade-to-lexical decision here is what keeps the API endpoint and the MCP tool from
    drifting into two slightly different fallbacks."""
    model = default_embedding_model()
    if not model:
        return None
    try:
        return embed_text(query, model)
    except EmbeddingError:
        return None


def search_signals(query: str, limit: int = 20, dsn: str | None = None) -> dict:
    """Full hybrid search from a bare query string: embed (or degrade), connect, fuse, hydrate.

    Returns {"query", "semantic", "hits"}. This is the non-pool entry point, shared by the MCP
    tool (api/mcp_server.py) so its search matches GET /search exactly, the same shared-query
    pattern get_rated_signals uses across the MCP and worker paths. The API endpoint doesn't call
    this, it uses its own connection pool, but goes through the same embed_query + hybrid_search.
    dsn overrides the connection target (default DATABASE_URL) for the same reason as
    get_rated_signals, a laptop runner points it at the real database explicitly."""
    query_embedding = embed_query(query)
    with psycopg.connect(dsn or DATABASE_URL) as conn:
        hits = hybrid_search(conn, query, query_embedding, limit=limit)
    return {"query": query, "semantic": query_embedding is not None, "hits": hits}
