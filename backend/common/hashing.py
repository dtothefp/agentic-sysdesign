"""content_hash lives in one place so the seed and the API produce identical hashes for
identical payloads. That identity is what makes the ON CONFLICT dedup work across both
paths: a payload seeded and later re-POSTed hashes the same, so it's a no-op."""

import hashlib
import json


def content_hash(payload: dict) -> str:
    """sha256 of the payload, with sorted keys so ordering never changes the hash."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
