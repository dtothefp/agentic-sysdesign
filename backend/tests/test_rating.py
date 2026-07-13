"""The rating adapter's pure logic: provider/model resolution (fail-at-the-door validation)
and the tolerant parser that turns small-model output into a validated rating."""
import pytest

from common.rating import RatingError, _parse_rating, default_model, resolve_model


def test_resolve_ollama_no_key(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    provider, model_name, base_url, api_key = resolve_model("ollama/llama3.2:1b")
    assert provider == "ollama"
    assert model_name == "llama3.2:1b"
    assert base_url == "http://localhost:11434/v1"
    assert api_key is None


def test_resolve_base_url_override_strips_slash(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434/v1/")
    _, _, base_url, _ = resolve_model("ollama/anything")
    assert base_url == "http://ollama:11434/v1"


def test_resolve_provider_key_from_env(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "sk-test-123")
    provider, _, _, api_key = resolve_model("groq/llama-3.1-8b-instant")
    assert provider == "groq"
    assert api_key == "sk-test-123"


def test_resolve_missing_key_raises(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError):
        resolve_model("deepseek/deepseek-chat")


@pytest.mark.parametrize("bad", ["", "noslash", "unknown/model", "ollama/", "/model"])
def test_resolve_bad_strings_raise(bad):
    with pytest.raises(ValueError):
        resolve_model(bad)


def test_default_model_reads_env_at_call_time(monkeypatch):
    monkeypatch.delenv("RATING_MODEL", raising=False)
    assert default_model() is None
    monkeypatch.setenv("RATING_MODEL", "ollama/llama3.2:1b")
    assert default_model() == "ollama/llama3.2:1b"


def test_parse_rating_plain_json():
    out = _parse_rating('{"relevance": 0.9, "confidence": 0.8, "topics": ["ai", "agents"], "summary": "About AI."}')
    assert out["relevance"] == 0.9
    assert out["confidence"] == 0.8
    assert out["topics"] == ["ai", "agents"]
    assert out["summary"] == "About AI."


def test_parse_rating_strips_fences_and_think_and_clamps():
    raw = '<think>let me think</think>```json\n{"relevance": 2, "confidence": -1, "topics": [], "summary": "x"}\n```'
    out = _parse_rating(raw)
    assert out["relevance"] == 1.0  # clamped to [0, 1]
    assert out["confidence"] == 0.0


def test_parse_rating_caps_topics_at_five():
    out = _parse_rating(
        '{"relevance": 0.5, "confidence": 0.5, "topics": ["a","b","c","d","e","f","g"], "summary": "s"}'
    )
    assert len(out["topics"]) == 5


def test_parse_rating_non_json_raises():
    with pytest.raises(RatingError):
        _parse_rating("this is not json at all")


def test_parse_rating_empty_summary_raises():
    with pytest.raises(RatingError):
        _parse_rating('{"relevance": 0.5, "confidence": 0.5, "topics": [], "summary": "   "}')
