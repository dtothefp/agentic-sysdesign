"""The /chat gate. Same inert-until-keyed contract as services/api: an unset (or empty)
SYSDESIGN_API_KEY means open (local dev), a set one means /chat needs a matching X-API-Key while
/health stays open (Railway's headerless healthcheck). require_api_key reads os.environ at call
time, so monkeypatch drives each case on one client, no reload. These assert at the gate only,
they never reach run_agent, so no network, no Anthropic, no spend.
"""

from __future__ import annotations

from agent.server import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_health_always_open(monkeypatch):
    monkeypatch.setenv("SYSDESIGN_API_KEY", "secret")
    assert client.get("/health").status_code == 200


def test_chat_rejects_missing_and_wrong_key(monkeypatch):
    monkeypatch.setenv("SYSDESIGN_API_KEY", "secret")
    assert client.post("/chat", json={"message": "hi"}).status_code == 401
    assert client.post("/chat", json={"message": "hi"}, headers={"X-API-Key": "wrong"}).status_code == 401


def test_chat_open_when_unkeyed(monkeypatch):
    # Empty key is falsy, so the gate treats it as unkeyed (open). A malformed body (missing the
    # required 'message') then trips validation: a 422, not a 401, proves the gate let it through.
    monkeypatch.setenv("SYSDESIGN_API_KEY", "")
    assert client.post("/chat", json={}).status_code == 422
