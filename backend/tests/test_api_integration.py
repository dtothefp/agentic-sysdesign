"""End-to-end HTTP tests through the real app + Postgres. Auto-skipped when the DB is
unreachable (see conftest.api_client). Exercises the one write path and the fail-at-the-door
validation, without needing the Celery worker or Redis."""

import pytest

pytestmark = pytest.mark.integration

# A fixed timestamp inside a partition the migrations always create (raw_signals is
# partitioned by month, 2026 only). Hard-coded rather than datetime.now() so the test is
# deterministic regardless of the wall clock in CI.
COVERED_CAPTURED_AT = "2026-07-15T12:00:00+00:00"


def test_health(api_client):
    r = api_client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_signal_roundtrip_is_idempotent(api_client):
    body = {
        "influencer_id": 1,  # seeded influencer
        "captured_at": COVERED_CAPTURED_AT,
        "payload": {"source": "pytest", "caption": "hello from the test suite"},
    }
    first = api_client.post("/signals", json=body)
    assert first.status_code == 200, first.text

    # the same payload POSTed again dedups via ON CONFLICT (inserted=False), same content_hash
    second = api_client.post("/signals", json=body)
    assert second.status_code == 200, second.text
    assert first.json()["content_hash"] == second.json()["content_hash"]
    assert second.json()["inserted"] is False


def test_signal_outside_any_partition_is_400(api_client):
    # partitions cover 2026 only; a 2020 timestamp has no partition, so the CheckViolation
    # surfaces as a 400 (provision the month or pick a covered captured_at).
    body = {
        "influencer_id": 1,
        "captured_at": "2020-01-15T00:00:00+00:00",
        "payload": {"source": "pytest", "caption": "no partition covers this"},
    }
    r = api_client.post("/signals", json=body)
    assert r.status_code == 400


def test_list_signals_requires_time_window(api_client):
    # from/to are required (they carry the partition key so the query always prunes)
    r = api_client.get("/signals")
    assert r.status_code == 422


def test_run_with_bad_model_rejected_at_the_door(api_client):
    # invalid provider/model is a 400 before any task is enqueued (no worker/Redis needed)
    r = api_client.post("/runs", json={"mode": "demo", "model": "nope/x"})
    assert r.status_code == 400
