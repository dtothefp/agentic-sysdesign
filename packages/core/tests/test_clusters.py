"""Module 6 discovery half: theme clustering. The cosine + greedy-cluster math is hermetic (pure
functions on small vectors, no infra), mirroring how test_search keeps RRF testable. The
get_signal_clusters end-to-end test needs a live Postgres with the module-6 schema and auto-skips
when it's unreachable or unmigrated, so `pytest` stays green anywhere."""

import os

import psycopg
import pytest
from common.clusters import cosine_distance, greedy_cluster
from common.embedding import from_vector_literal

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://lab:lab@localhost:5432/sysdesign")


# --- pure cosine distance (no infra) ---------------------------------------------


def test_cosine_distance_identical_is_zero():
    assert cosine_distance([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(0.0)


def test_cosine_distance_scale_invariant():
    # cosine ignores magnitude, only direction. A vector and its scaled self are distance 0.
    assert cosine_distance([1.0, 0.0], [5.0, 0.0]) == pytest.approx(0.0)


def test_cosine_distance_orthogonal_is_one():
    assert cosine_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)


def test_cosine_distance_opposite_is_two():
    assert cosine_distance([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(2.0)


def test_cosine_distance_zero_vector_is_max():
    # a zero vector has no direction; we return 1.0 rather than divide by zero.
    assert cosine_distance([0.0, 0.0], [1.0, 1.0]) == 1.0


# --- pure greedy clustering (no infra) -------------------------------------------


def _item(h, rel, vec, topics=None):
    return {"content_hash": h, "relevance": rel, "vector": vec, "topics": topics or []}


def test_greedy_cluster_separates_two_tight_groups():
    # two clearly separated directions -> two clusters at a moderate threshold.
    items = [
        _item("a", 0.9, [1.0, 0.0]),
        _item("b", 0.8, [0.99, 0.01]),
        _item("x", 0.7, [0.0, 1.0]),
        _item("y", 0.6, [0.01, 0.99]),
    ]
    clusters = greedy_cluster(items, threshold=0.1)
    assert len(clusters) == 2
    assert {m["content_hash"] for m in clusters[0]} == {"a", "b"}
    assert {m["content_hash"] for m in clusters[1]} == {"x", "y"}


def test_greedy_cluster_loose_threshold_merges_all():
    items = [_item("a", 0.9, [1.0, 0.0]), _item("x", 0.7, [0.0, 1.0])]
    # threshold 2.0 admits everything (max cosine distance is 2.0), so one cluster.
    clusters = greedy_cluster(items, threshold=2.0)
    assert len(clusters) == 1


def test_greedy_cluster_tight_threshold_all_singletons():
    items = [_item("a", 0.9, [1.0, 0.0]), _item("b", 0.8, [0.7, 0.7]), _item("x", 0.7, [0.0, 1.0])]
    # only identical directions merge; these three differ, so three singleton clusters.
    clusters = greedy_cluster(items, threshold=0.0)
    assert len(clusters) == 3


def test_greedy_cluster_seed_is_highest_relevance():
    # the representative (cluster[0]) must be the strongest post, seeding is relevance-ordered.
    items = [
        _item("low", 0.4, [1.0, 0.0]),
        _item("high", 0.95, [0.99, 0.01]),
        _item("mid", 0.6, [0.98, 0.02]),
    ]
    clusters = greedy_cluster(items, threshold=0.5)
    assert len(clusters) == 1
    assert clusters[0][0]["content_hash"] == "high"


def test_greedy_cluster_deterministic():
    items = [_item("a", 0.5, [1.0, 0.0]), _item("b", 0.5, [0.0, 1.0])]
    first = greedy_cluster(items, threshold=0.1)
    second = greedy_cluster(items, threshold=0.1)
    assert [[m["content_hash"] for m in c] for c in first] == [[m["content_hash"] for m in c] for c in second]


def test_greedy_cluster_empty():
    assert greedy_cluster([], threshold=0.3) == []


# --- from_vector_literal round-trips with to_vector_literal ----------------------


def test_from_vector_literal_parses_pgvector_text():
    assert from_vector_literal("[0.5,-1.0,2.0]") == [0.5, -1.0, 2.0]


def test_from_vector_literal_empty():
    assert from_vector_literal("[]") == []


# --- integration: real Postgres + module-6 schema, auto-skipped when unreachable -


def _db_reachable() -> bool:
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def _has_schema(conn) -> bool:
    return bool(
        conn.execute("SELECT to_regclass('signal_embeddings')").fetchone()[0]
        and conn.execute("SELECT to_regclass('signal_ratings')").fetchone()[0]
    )


@pytest.fixture()
def cluster_db():
    if not _db_reachable():
        pytest.skip("Postgres not reachable at DATABASE_URL")
    conn = psycopg.connect(DATABASE_URL)
    if not _has_schema(conn):
        conn.close()
        pytest.skip("module-6 schema (signal_embeddings + signal_ratings) not applied to this DB")
    conn.close()


def test_get_signal_clusters_shape(cluster_db):
    # runs against whatever data is present; asserts the documented shape, not specific themes,
    # so it's safe on any populated or empty schema-having database.
    from common.clusters import get_signal_clusters

    result = get_signal_clusters(days=3650, min_relevance=0.0, dsn=DATABASE_URL)
    assert set(result) >= {"clustered", "rated_in_window", "embedded", "theme_count", "themes"}
    assert isinstance(result["clustered"], bool)
    assert result["theme_count"] == len(result["themes"])
    for theme in result["themes"]:
        assert set(theme) >= {"theme_size", "avg_relevance", "topics", "representative", "members"}
        assert theme["theme_size"] == len(theme["members"])
        assert theme["representative"]["content_hash"] == theme["members"][0]["content_hash"]
    # themes are ordered biggest-first
    sizes = [t["theme_size"] for t in result["themes"]]
    assert sizes == sorted(sizes, reverse=True)
