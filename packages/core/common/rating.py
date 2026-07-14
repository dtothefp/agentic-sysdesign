"""The provider-agnostic rating adapter. One OpenAI-compatible chat completions call, raw.

The design rule this module implements, own the interface, rent the model. Every serving
stack we care about (Ollama locally, DeepSeek, Groq, OpenRouter, Anthropic's compatibility
endpoint) answers the same HTTP shape, POST {base_url}/chat/completions with {model,
messages} returning choices[0].message.content. It's a wire format everyone cloned from
OpenAI, the way S3's API got cloned by R2 and MinIO, so swapping providers is a base-url
and model-name change, never a code change.

Deliberately raw urllib, no OpenAI SDK, for the same reason the repo keeps raw SQL over an
ORM. The wire format is the studyable artifact, and an SDK would hide the one thing this
module exists to teach. (scrape.py already set the urllib precedent for Apify.)

Model strings are "provider/model", e.g. "ollama/qwen3:4b" or "deepseek/deepseek-chat".
Selection is data (it rides POST /runs and the runs row); this module owns only resolution,
the HTTP call, and parsing. If no model is given and RATING_MODEL isn't set in the env, the
rating pipeline is simply inert, which is what keeps prod safe until a provider key exists.

Structured output, honestly. Providers diverge exactly here: OpenAI-style json_schema
enforcement isn't universal, so we use the lowest common denominator that actually holds up,
response_format {"type": "json_object"} where supported, the schema spelled out in the
system prompt, and strict parse-and-validate on our side. The validator, not the vendor, is
the contract.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

# Observability, LangSmith. The one model call in this module is a natural choke point, so a
# single @traceable there captures every rating's prompt, output, latency, and errors. This
# does NOT touch the raw-urllib design, LangSmith is a tracer, not an LLM SDK, so the wire
# format stays the studyable artifact. It's a no-op until LANGSMITH_TRACING=true and a key are
# in the env (same inert-until-keyed contract as the rating layer itself). Soft-imported so a
# venv without the package still runs, the decorator just degrades to a passthrough.
try:
    from langsmith import traceable
    from langsmith.run_helpers import get_current_run_tree
except ImportError:  # pragma: no cover - tracing is optional

    def traceable(*d_args: Any, **d_kwargs: Any):  # type: ignore[misc]
        # support both bare @traceable and @traceable(run_type=..., ...)
        if len(d_args) == 1 and callable(d_args[0]) and not d_kwargs:
            return d_args[0]
        return lambda fn: fn

    def get_current_run_tree():  # type: ignore[misc]
        return None


# Registry of OpenAI-compatible providers. base_url can be overridden per provider with
# <PROVIDER>_BASE_URL (the devcontainer sets OLLAMA_BASE_URL=http://ollama:11434/v1).
# json_mode=False means the provider's compatibility layer doesn't accept response_format,
# so we rely on the prompt + our validator alone.
PROVIDERS: dict[str, dict[str, Any]] = {
    "ollama": {"base_url": "http://localhost:11434/v1", "key_env": None, "json_mode": True},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "key_env": "DEEPSEEK_API_KEY", "json_mode": True},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY", "json_mode": True},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "key_env": "OPENROUTER_API_KEY", "json_mode": True},
    "anthropic": {"base_url": "https://api.anthropic.com/v1", "key_env": "ANTHROPIC_API_KEY", "json_mode": False},
}

SYSTEM_PROMPT = (
    "You rate Instagram posts from AI/tech creators for an AI-research intelligence feed. "
    "Relevance means: AI tooling, agents, LLM infrastructure, orchestration, developer "
    "workflows, or the business of building with AI. Respond with ONLY a json object, no "
    "prose, exactly this shape: "
    '{"relevance": <float 0-1>, "confidence": <float 0-1>, '
    '"topics": [<up to 5 short lowercase strings>], "summary": "<one sentence>"}'
)


class RatingError(RuntimeError):
    """Any failure between us and a parsed, valid rating. Retryable by the caller."""


def default_model() -> str | None:
    """The worker's default model, or None, in which case rating is disabled. Read at call
    time, not import time, so tests and the env manifest can flip it without reimporting."""
    return os.environ.get("RATING_MODEL") or None


def resolve_model(model: str) -> tuple[str, str, str, str | None]:
    """'provider/model' -> (provider, model_name, base_url, api_key).

    Raises ValueError on an unknown provider or a missing key, so the API can reject a bad
    POST /runs with a 400 before any task is enqueued (fail at the door, not in the worker).
    """
    provider, sep, model_name = model.partition("/")
    if not sep or not model_name or provider not in PROVIDERS:
        raise ValueError(f"model must be 'provider/model' with provider one of {sorted(PROVIDERS)}, got {model!r}")
    cfg = PROVIDERS[provider]
    base_url = os.environ.get(f"{provider.upper()}_BASE_URL", cfg["base_url"]).rstrip("/")
    api_key = os.environ.get(cfg["key_env"]) if cfg["key_env"] else None
    if cfg["key_env"] and not api_key:
        raise ValueError(f"model {model!r} needs {cfg['key_env']} in the environment")
    return provider, model_name, base_url, api_key


def _parse_rating(content: str) -> dict[str, Any]:
    """Model output -> validated rating dict. Tolerates the two ways small models misbehave
    (markdown ```json fences, and Qwen-style <think>...</think> preambles), then enforces
    the schema ourselves: clamp the floats, cap topics at 5, require a summary."""
    text = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise RatingError(f"model returned non-JSON: {content[:200]!r}") from e
    try:
        rating = {
            "relevance": min(1.0, max(0.0, float(raw["relevance"]))),
            "confidence": min(1.0, max(0.0, float(raw["confidence"]))),
            "topics": [str(t)[:64] for t in (raw.get("topics") or [])][:5],
            "summary": str(raw["summary"]).strip()[:500],
        }
    except (KeyError, TypeError, ValueError) as e:
        raise RatingError(f"model JSON missing/invalid fields: {text[:200]!r}") from e
    if not rating["summary"]:
        raise RatingError("model returned an empty summary")
    return rating


@traceable(run_type="llm", name="rate_caption")
def _chat_completion(messages: list[dict[str, str]], model: str, timeout: int = 180) -> dict[str, Any]:
    """The one model call, isolated so LangSmith renders it as a proper LLM run.

    This is the traced boundary, not rate_caption, for one specific reason: LangSmith shows a
    run's prompt only when the traced INPUTS carry a `messages` list, and shows the completion
    only when the OUTPUT is the OpenAI response shape (choices[].message). So the traced function
    takes `messages` in and returns the raw response, which is exactly what makes the Tracing
    view display the system+user prompt and the model's reply. Resolving the api_key INSIDE (it's
    never a parameter) keeps the secret out of the trace inputs.

    Inert unless LANGSMITH_TRACING=true and a key are set (@traceable degrades to a passthrough).
    """
    provider, model_name, base_url, api_key = resolve_model(model)
    body: dict[str, Any] = {"model": model_name, "temperature": 0, "messages": messages}
    if PROVIDERS[provider]["json_mode"]:
        body["response_format"] = {"type": "json_object"}

    # The User-Agent matters. urllib's default (Python-urllib/3.x) trips Cloudflare's bot
    # fingerprinting in front of Groq, which answers 403 "error code: 1010" before the
    # request ever reaches the API. Same class of gotcha as Railway's GraphQL endpoint
    # (infra scripts send curl's UA for the same reason). Any honest non-default UA passes.
    headers = {"Content-Type": "application/json", "User-Agent": "sysdesign-rating/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(f"{base_url}/chat/completions", data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RatingError(f"{provider} {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise RatingError(f"{provider} unreachable at {base_url}: {e}") from None

    # Feed token counts to LangSmith so its dashboard shows tokens and $ per rating. An SDK
    # response would surface these automatically; with raw urllib we hand them over ourselves.
    # OpenAI-compatible usage keys map onto LangSmith's usage_metadata shape; no-op when tracing
    # is off (get_current_run_tree() is None) or the provider omitted usage.
    _record_usage(resp.get("usage"), provider, model_name)
    return resp


def rate_caption(handle: str, caption: str, model: str, timeout: int = 180) -> dict[str, Any]:
    """One rating: build the prompt, call the model (traced), validate the reply.

    The generous timeout is for the CPU-inference case (local Ollama streams a handful of
    tokens per second). Hosted GPU providers answer in a second or two.
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Creator: @{handle}\nCaption:\n{caption or '(no caption)'}"},
    ]
    resp = _chat_completion(messages, model, timeout)
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RatingError(f"unexpected response shape: {str(resp)[:200]!r}") from None
    return _parse_rating(content)


def _record_usage(usage: dict[str, Any] | None, provider: str, model_name: str) -> None:
    """Attach OpenAI-style usage to the active LangSmith run, if there is one.

    ls_model_name lets LangSmith match its price table for the $ column; provider is just a
    filterable tag. usage_metadata in the run's outputs is the schema LangSmith reads token
    counts from (an SDK response would populate it for us)."""
    run = get_current_run_tree()
    if run is None:
        return
    run.add_metadata({"provider": provider, "ls_model_name": model_name})
    if not usage:
        return
    run.add_outputs(
        {
            "usage_metadata": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
        }
    )


def insert_rating(conn, content_hash: str, model: str, rating: dict[str, Any]) -> bool:
    """Idempotent write, same ON CONFLICT story as insert_signal. First rating for a hash
    wins; a concurrent duplicate (sweep racing a scrape-enqueued task) is a no-op. Returns
    whether this call inserted."""
    cur = conn.execute(
        "INSERT INTO signal_ratings (content_hash, model, relevance, confidence, topics, summary) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (content_hash) DO NOTHING",
        (content_hash, model, rating["relevance"], rating["confidence"], rating["topics"], rating["summary"]),
    )
    return cur.rowcount == 1
