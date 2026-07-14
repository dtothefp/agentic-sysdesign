"""Module 6 hybrid search. The fusion math and the adapter's pure logic are hermetic (no infra);
the FTS/vector/RRF end-to-end tests need a live Postgres with pgvector and auto-skip when it's
unreachable, so `pytest` stays green anywhere (same split as test_rating / conftest)."""

import os

import psycopg
import pytest
from common.embedding import (
    EMBEDDING_DIM,
    default_embedding_model,
    resolve_embedding_model,
    to_vector_literal,
)
from common.search import reciprocal_rank_fusion

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://lab:lab@localhost:5432/sysdesign")


# --- pure RRF fusion (no infra) --------------------------------------------------


def test_rrf_single_list_preserves_order():
    # one list: fused order == input order, scores strictly decreasing by rank
    fused = reciprocal_rank_fusion([["a", "b", "c"]], k=60)
    assert [doc for doc, _ in fused] == ["a", "b", "c"]
    scores = [s for _, s in fused]
    assert scores == sorted(scores, reverse=True)


def test_rrf_agreement_beats_single_list_top():
    # 'b' is rank 2 in both lists; 'a' and 'x' are each rank 1 in only one list. Appearing in
    # BOTH lists (even at rank 2) should outscore a single rank-1 appearance. That's the point.
    fused = dict(reciprocal_rank_fusion([["a", "b"], ["x", "b"]], k=60))
    assert fused["b"] > fused["a"]
    assert fused["b"] > fused["x"]


def test_rrf_score_matches_closed_form():
    # 'a' is rank 1 in list one and rank 2 in list two -> 1/(k+1) + 1/(k+2)
    k = 60
    fused = dict(reciprocal_rank_fusion([["a", "b"], ["b", "a"]], k=k))
    assert fused["a"] == pytest.approx(1 / (k + 1) + 1 / (k + 2))
    assert fused["a"] == pytest.approx(fused["b"])  # symmetric ranks -> equal scores


def test_rrf_k_dampens_top_rank_dominance():
    # a larger k flattens the curve, so rank-1's lead over rank-2 shrinks.
    small_k = dict(reciprocal_rank_fusion([["a", "b"]], k=1))
    large_k = dict(reciprocal_rank_fusion([["a", "b"]], k=1000))
    assert (small_k["a"] - small_k["b"]) > (large_k["a"] - large_k["b"])


def test_rrf_empty():
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[]]) == []


def test_rrf_deterministic_tiebreak():
    # equal scores fall back to id order, so results are reproducible (stable pagination)
    fused = reciprocal_rank_fusion([["b", "a"], ["a", "b"]], k=60)
    assert [doc for doc, _ in fused] == ["a", "b"]


# --- embedding adapter pure logic (no infra) -------------------------------------


def test_resolve_embedding_ollama_no_key(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    provider, model_name, base_url, api_key = resolve_embedding_model("ollama/nomic-embed-text")
    assert provider == "ollama"
    assert model_name == "nomic-embed-text"
    assert base_url == "http://localhost:11434/v1"
    assert api_key is None


def test_resolve_embedding_missing_key_raises(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError):
        resolve_embedding_model("openai/text-embedding-3-small")


@pytest.mark.parametrize("bad", ["", "noslash", "unknown/model", "openai/", "/model"])
def test_resolve_embedding_bad_strings_raise(bad):
    with pytest.raises(ValueError):
        resolve_embedding_model(bad)


def test_default_embedding_model_reads_env_at_call_time(monkeypatch):
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    assert default_embedding_model() is None
    monkeypatch.setenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")
    assert default_embedding_model() == "openai/text-embedding-3-small"


def test_to_vector_literal_shape():
    # pgvector requires the bracketed, comma-separated form; anything else errors on cast.
    assert to_vector_literal([0.5, -1.0, 2]) == "[0.5,-1.0,2.0]"


def test_embedding_dim_is_the_schema_fixed_width():
    # guards the coupling: signal_embeddings.embedding is vector(1536).
    assert EMBEDDING_DIM == 1536


# --- integration: real Postgres + pgvector, auto-skipped when unreachable --------


def _db_reachable() -> bool:
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def _has_hybrid_schema(conn) -> bool:
    # the module-6 migration must have run: caption_tsv column + signal_embeddings table.
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'raw_signals' AND column_name = 'caption_tsv'"
        ).fetchone()
        and conn.execute("SELECT to_regclass('signal_embeddings')").fetchone()[0]
    )


@pytest.fixture()
def hybrid_conn():
    """A connection to a database that has the module-6 schema, or a skip. Read-only tests below
    query whatever data is present; they assert on structure/ranking behavior, not specific rows,
    so they're safe against any populated dev or scratch database."""
    if not _db_reachable():
        pytest.skip("Postgres not reachable at DATABASE_URL")
    conn = psycopg.connect(DATABASE_URL)
    if not _has_hybrid_schema(conn):
        conn.close()
        pytest.skip("module-6 migration (caption_tsv + signal_embeddings) not applied to this DB")
    try:
        yield conn
    finally:
        conn.close()


def test_fts_query_runs_and_ranks(hybrid_conn):
    # the generated tsvector + GIN + websearch_to_tsquery + ts_rank_cd pipeline executes and
    # returns rows in non-increasing rank order (structure test, independent of which rows exist).
    rows = hybrid_conn.execute(
        "SELECT ts_rank_cd(caption_tsv, q) AS rank "
        "FROM raw_signals, websearch_to_tsquery('english', 'the') q "
        "WHERE caption_tsv @@ q ORDER BY rank DESC LIMIT 5"
    ).fetchall()
    ranks = [r[0] for r in rows]
    assert ranks == sorted(ranks, reverse=True)


def test_vector_knn_uses_hnsw_and_orders_by_distance(hybrid_conn):
    # if there are >=2 embeddings, a self-probe returns itself at distance 0 first, and the
    # planner picks the HNSW index for the ORDER BY <=> ... LIMIT shape.
    probe = hybrid_conn.execute("SELECT content_hash FROM signal_embeddings LIMIT 1").fetchone()
    if probe is None:
        pytest.skip("no embeddings present to probe")
    ch = probe[0]
    rows = hybrid_conn.execute(
        "SELECT e.content_hash, e.embedding <=> me.embedding AS dist "
        "FROM signal_embeddings e "
        "CROSS JOIN (SELECT embedding FROM signal_embeddings WHERE content_hash = %s) me "
        "ORDER BY e.embedding <=> me.embedding LIMIT 5",
        (ch,),
    ).fetchall()
    assert rows[0][0] == ch and rows[0][1] == pytest.approx(0.0, abs=1e-6)
    dists = [r[1] for r in rows]
    assert dists == sorted(dists)

    plan = "\n".join(
        r[0]
        for r in hybrid_conn.execute(
            "EXPLAIN SELECT content_hash FROM signal_embeddings "
            "ORDER BY embedding <=> (SELECT embedding FROM signal_embeddings WHERE content_hash = %s) LIMIT 5",
            (ch,),
        ).fetchall()
    )
    assert "signal_embeddings_hnsw" in plan


def test_hybrid_search_lexical_only_shape(hybrid_conn):
    # lexical-only (query_embedding=None) works with no provider and returns the documented shape.
    from common.search import hybrid_search

    hits = hybrid_search(hybrid_conn, "the", None, limit=5)
    for h in hits:
        assert set(h) >= {"content_hash", "score", "sources"}
        assert h["sources"] == ["lexical"]  # semantic half didn't run
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)
