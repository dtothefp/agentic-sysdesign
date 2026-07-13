"""content_hash is the dedup key shared by the API and the worker, so its determinism and
key-order independence are load-bearing for the ON CONFLICT idempotency story."""

from common.hashing import content_hash


def test_deterministic():
    payload = {"a": 1, "b": "x"}
    assert content_hash(payload) == content_hash(payload)


def test_key_order_independent():
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})


def test_distinct_payloads_differ():
    assert content_hash({"a": 1}) != content_hash({"a": 2})


def test_is_sha256_hex():
    h = content_hash({"a": 1})
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)
