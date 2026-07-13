"""The pydantic edge models: defaults the API relies on and the validation that rejects
malformed payloads before a handler ever sees them."""

import pytest
from pydantic import ValidationError

from api.models import RunCreated, RunTrigger, SignalIn


def test_runtrigger_defaults():
    t = RunTrigger()
    assert t.mode == "live"
    assert t.limit == 5
    assert t.model is None


def test_runtrigger_demo_mode():
    t = RunTrigger(mode="demo", limit=3, model="ollama/llama3.2:1b")
    assert t.mode == "demo"
    assert t.limit == 3
    assert t.model == "ollama/llama3.2:1b"


def test_runtrigger_rejects_bad_mode():
    with pytest.raises(ValidationError):
        RunTrigger(mode="bogus")


def test_signalin_requires_fields():
    with pytest.raises(ValidationError):
        SignalIn(influencer_id=1)  # missing captured_at and payload


def test_runcreated_optional_model_defaults_none():
    rc = RunCreated(run_id=1, total=5, mode="demo")
    assert rc.model is None
