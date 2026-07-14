"""The provider-agnostic embedding adapter. One OpenAI-compatible embeddings call, raw.

Same design rule as common/rating.py, own the interface, rent the model. Every serving stack
that produces embeddings answers the same HTTP shape, POST {base_url}/embeddings with {model,
input} returning data[0].embedding. It's the wire format OpenAI defined and everyone cloned
(Ollama, Together, Voyage, Mistral all speak it), so swapping providers is a base-url and
model-name change, never a code change. Deliberately raw urllib, no SDK, so the wire format
stays the studyable artifact, exactly like rating.py.

This module is the shared substrate for TWO Module-6/Module-4 features:

  * hybrid SEARCH (common/search.py), the semantic half of the ranked lists RRF fuses.
  * the rating SEMANTIC CACHE (worker/tasks.py), serve a prior rating when a new caption's
    embedding is near-identical to an already-rated one, skipping the ~100x pricier chat call.

Both read the ONE signal_embeddings table: an embedding computed for search doubles as the
rating cache key, and vice-versa. Compute a vector once, use it for both.

Model strings are "provider/model", e.g. "openai/text-embedding-3-small" or
"ollama/nomic-embed-text". If EMBEDDING_MODEL isn't set the whole embedding path is inert
(search runs lexical-only, rating never caches), the same inert-until-keyed contract the
rating layer uses to stay safe in prod until a provider key exists.

The dimension is fixed. signal_embeddings.embedding is vector(1536) (OpenAI
text-embedding-3-small's native size), so a model that returns a different width is rejected
here with a clear message rather than deep in Postgres. The validator, not the vendor, is the
contract, same as the rating parser.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

# LangSmith tracing, soft-imported and inert until LANGSMITH_TRACING=true + a key are set,
# exactly as in rating.py. The one embeddings call is the natural choke point to trace.
try:
    from langsmith import traceable
    from langsmith.run_helpers import get_current_run_tree
except ImportError:  # pragma: no cover - tracing is optional

    def traceable(*d_args: Any, **d_kwargs: Any):  # type: ignore[misc]
        if len(d_args) == 1 and callable(d_args[0]) and not d_kwargs:
            return d_args[0]
        return lambda fn: fn

    def get_current_run_tree():  # type: ignore[misc]
        return None


# signal_embeddings.embedding is vector(1536). This is the one hard constraint the schema
# imposes on which models are usable: a 768-dim model (ollama/nomic-embed-text) or a 1024-dim
# one (mistral-embed) can't fill this column. Changing it is a migration, not a config flip,
# because HNSW indexes a fixed-width vector. text-embedding-3-small is the natural 1536 match.
EMBEDDING_DIM = 1536

# Registry of OpenAI-compatible embedding providers. base_url overridable per provider with
# <PROVIDER>_BASE_URL (the devcontainer sets OLLAMA_BASE_URL). native_dim is the model's usual
# width, documented so a mismatch with EMBEDDING_DIM is obvious at a glance; it's not enforced
# from here (the model can return whatever), the runtime check on the actual response is.
PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {"base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY"},  # 3-small = 1536
    "ollama": {"base_url": "http://localhost:11434/v1", "key_env": None},  # dims vary by model
    "together": {"base_url": "https://api.together.xyz/v1", "key_env": "TOGETHER_API_KEY"},
    "voyage": {"base_url": "https://api.voyageai.com/v1", "key_env": "VOYAGE_API_KEY"},
    "mistral": {"base_url": "https://api.mistral.ai/v1", "key_env": "MISTRAL_API_KEY"},
}


class EmbeddingError(RuntimeError):
    """Any failure between us and a valid EMBEDDING_DIM-wide vector. Retryable by the caller."""


def default_embedding_model() -> str | None:
    """The default embedding model, or None (embedding disabled). Read at call time, not import
    time, so the env manifest and tests can flip it without reimporting. None here is what keeps
    search lexical-only and the rating cache off until a provider is configured."""
    return os.environ.get("EMBEDDING_MODEL") or None


def resolve_embedding_model(model: str) -> tuple[str, str, str, str | None]:
    """'provider/model' -> (provider, model_name, base_url, api_key).

    Raises ValueError on an unknown provider or a missing key, so a bad EMBEDDING_MODEL fails
    at startup / the door rather than on the first embedding call, same as resolve_model."""
    provider, sep, model_name = model.partition("/")
    if not sep or not model_name or provider not in PROVIDERS:
        raise ValueError(f"embedding model must be 'provider/model' with provider one of {sorted(PROVIDERS)}, got {model!r}")
    cfg = PROVIDERS[provider]
    base_url = os.environ.get(f"{provider.upper()}_BASE_URL", cfg["base_url"]).rstrip("/")
    api_key = os.environ.get(cfg["key_env"]) if cfg["key_env"] else None
    if cfg["key_env"] and not api_key:
        raise ValueError(f"embedding model {model!r} needs {cfg['key_env']} in the environment")
    return provider, model_name, base_url, api_key


def to_vector_literal(vector: list[float]) -> str:
    """A Python float list -> pgvector's text input format '[0.1,0.2,...]'.

    We store and query vectors as text literals cast with ::vector rather than pulling in the
    pgvector-python adapter, the same raw-over-library choice the repo makes for SQL. pgvector
    parses this exact bracketed, comma-separated shape; anything else errors with 'Vector
    contents must start with "["'."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def from_vector_literal(literal: str) -> list[float]:
    """The inverse of to_vector_literal. pgvector's text output '[0.1,0.2,...]' -> a float list.

    Postgres returns a `vector` column cast to text (or read without the pgvector adapter) in this
    bracketed shape. We parse it by hand rather than register the adapter, the same raw-over-library
    choice as to_vector_literal, so the clustering layer can pull embeddings back into Python."""
    inner = literal.strip().strip("[]")
    if not inner:
        return []
    return [float(x) for x in inner.split(",")]


@traceable(run_type="embedding", name="embed_text")
def _embeddings_call(text: str, model: str, timeout: int = 60) -> list[float]:
    """The one embeddings call, isolated so LangSmith renders it as a proper embedding run.

    Resolving the api_key INSIDE (never a parameter) keeps the secret out of the trace inputs,
    same reasoning as _chat_completion. Returns the raw float list; the caller validates width."""
    provider, model_name, base_url, api_key = resolve_embedding_model(model)
    body = {"model": model_name, "input": text}

    # Non-default User-Agent for the same reason as rating.py: urllib's default UA trips some
    # providers' bot fingerprinting (Cloudflare 1010) before the request reaches the API.
    headers = {"Content-Type": "application/json", "User-Agent": "sysdesign-embedding/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(f"{base_url}/embeddings", data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise EmbeddingError(f"{provider} {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise EmbeddingError(f"{provider} unreachable at {base_url}: {e}") from None

    try:
        vector = resp["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError):
        raise EmbeddingError(f"unexpected embeddings response shape: {str(resp)[:200]!r}") from None
    _record_usage(resp.get("usage"), provider, model_name)
    return [float(x) for x in vector]


def embed_text(text: str, model: str, timeout: int = 60) -> list[float]:
    """One embedding: call the provider (traced), enforce the fixed width.

    The dimension check is the fail-at-the-door contract: signal_embeddings is vector(1536),
    so a wrong-width vector can never reach the column, and the error names the mismatch instead
    of surfacing Postgres's opaque 'expected 1536 dimensions, not N'."""
    vector = _embeddings_call(text or "(empty)", model, timeout)
    if len(vector) != EMBEDDING_DIM:
        raise EmbeddingError(
            f"model {model!r} returned {len(vector)} dims, but signal_embeddings is fixed at "
            f"{EMBEDDING_DIM}. Use a {EMBEDDING_DIM}-dim model (e.g. openai/text-embedding-3-small)."
        )
    return vector


def _record_usage(usage: dict[str, Any] | None, provider: str, model_name: str) -> None:
    """Attach OpenAI-style usage to the active LangSmith run, if any. No-op when tracing is off
    or the provider omitted usage (Ollama does). Embeddings report prompt_tokens only."""
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
                "total_tokens": usage.get("total_tokens", usage.get("prompt_tokens", 0)),
            }
        }
    )


def insert_embedding(conn, content_hash: str, model: str, vector: list[float]) -> bool:
    """Idempotent write, same ON CONFLICT story as insert_rating / insert_signal. First
    embedding for a hash wins; a concurrent duplicate (the search sweep racing the rating
    cache, both wanting this hash's vector) is a no-op. Returns whether this call inserted.

    The vector rides as a ::vector-cast text literal (see to_vector_literal), so no pgvector
    adapter is needed and the SQL stays readable."""
    cur = conn.execute(
        "INSERT INTO signal_embeddings (content_hash, model, embedding) VALUES (%s, %s, %s::vector) "
        "ON CONFLICT (content_hash) DO NOTHING",
        (content_hash, model, to_vector_literal(vector)),
    )
    return cur.rowcount == 1
