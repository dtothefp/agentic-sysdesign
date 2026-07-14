"""Local secret loader. One place that teaches the workspace-root .env to the process.

The dev-container worker and the host API both read secrets from os.environ, but nothing
populates os.environ from .env except the one-off loader scrape.py wrote for the Apify
key. This generalizes that: on import, fill in any key that the root .env defines and
the environment doesn't already have.

Env-first is the whole contract. A value already in os.environ (set by the dev-container's
compose.yml, by Railway, or exported in the shell) always wins; .env only fills the gaps.
So DATABASE_URL / REDIS_URL / RATING_MODEL from compose are never clobbered, and prod (where
there's no .env file on disk) is a silent no-op. This is why importing it at the top of
common.db is safe: it runs before DATABASE_URL is read, but can't override it.

Deliberately a hand-rolled parser, not python-dotenv, to keep the dependency list honest and
match the raw-urllib / raw-SQL spirit of the rest of the codebase. It handles KEY=VALUE, skips
blanks and # comments, and strips an optional surrounding pair of quotes.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_local_env() -> None:
    """Populate os.environ from the workspace root's .env, without overriding what's set.

    The workspace root is the first parent holding a `.moon` dir, NOT the first parent
    holding a `.env`: this repo can live nested inside a larger workspace whose own root
    .env carries unrelated credentials, and a bare .env search would happily walk up into
    it. Anchoring on the moon marker stops the walk at this repo's boundary."""
    for base in [Path.cwd(), *Path(__file__).resolve().parents]:
        if not (base / ".moon").is_dir():
            continue
        env_file = base / ".env"
        if not env_file.exists():
            return  # found the workspace root but it has no .env: prod, silent no-op
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)  # env-first: never override an existing value
        return  # first workspace root found wins
