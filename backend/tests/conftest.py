"""Shared fixtures. The unit tests are hermetic (no infra); the integration tests need a
live Postgres and are auto-skipped when it's unreachable, so `pytest` stays green anywhere."""

import os

import psycopg
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://lab:lab@localhost:5432/sysdesign")


def _db_reachable() -> bool:
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def api_client():
    """FastAPI TestClient with the app lifespan run (so the connection pool is opened).

    Skips (rather than fails) when Postgres isn't reachable, and clears SYSDESIGN_API_KEY so
    the data routes stay open for the test regardless of the ambient environment.
    """
    if not _db_reachable():
        pytest.skip("Postgres not reachable at DATABASE_URL; skipping integration tests")

    prior_key = os.environ.pop("SYSDESIGN_API_KEY", None)
    from fastapi.testclient import TestClient

    from api.main import app

    try:
        with TestClient(app) as client:
            yield client
    finally:
        if prior_key is not None:
            os.environ["SYSDESIGN_API_KEY"] = prior_key
