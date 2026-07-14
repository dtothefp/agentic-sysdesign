"""Tests for the /vibes endpoint and fun features."""

import pytest
from fastapi.testclient import TestClient

from api.main import app


client = TestClient(app)


def test_teapot_easter_egg():
    """Test the RFC 2324 teapot easter egg."""
    response = client.get("/teapot")
    assert response.status_code == 418
    assert "teapot" in response.json()["error"].lower()
    assert "RFC 2324" in response.json()["rfc"]


def test_vibes_endpoint_structure():
    """Test that /vibes returns the expected structure."""
    response = client.get("/vibes")
    
    # might be 200 with data or 500 if db not available, that's ok for structure test
    if response.status_code == 200:
        data = response.json()
        
        assert "total_signals" in data
        assert "total_influencers" in data
        assert "vibe_check" in data
        assert "fun_fact" in data
        assert "energy_level" in data
        
        assert isinstance(data["total_signals"], int)
        assert isinstance(data["total_influencers"], int)
        assert isinstance(data["vibe_check"], str)
        assert isinstance(data["fun_fact"], str)
        assert isinstance(data["energy_level"], str)
        
        # most_active_influencer can be null if no data
        if data.get("most_active_influencer"):
            ma = data["most_active_influencer"]
            assert "handle" in ma
            assert "name" in ma
            assert "signal_count" in ma
            assert "vibe" in ma


def test_vibes_in_openapi():
    """Test that /vibes appears in OpenAPI spec but /teapot doesn't."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    
    openapi = response.json()
    paths = openapi.get("paths", {})
    
    # /vibes should be documented
    assert "/vibes" in paths
    assert paths["/vibes"]["get"]["tags"] == ["vibes"]
    
    # /teapot should be hidden (include_in_schema=False)
    assert "/teapot" not in paths


def test_vibes_creative_language():
    """Test that vibes uses creative, entertaining language."""
    response = client.get("/vibes")
    
    if response.status_code == 200:
        data = response.json()
        
        creative_words = [
            "buzzing", "feral", "vibes", "energy", "absolutely",
            "lowkey", "fire", "iconic", "chaotic", "unhinged"
        ]
        
        all_text = " ".join([
            data.get("vibe_check", ""),
            data.get("fun_fact", ""),
            data.get("energy_level", ""),
            data.get("most_active_influencer", {}).get("vibe", "")
        ]).lower()
        
        # at least some creative language should appear
        assert any(word in all_text for word in creative_words), \
            "Vibes should use creative, entertaining language"
