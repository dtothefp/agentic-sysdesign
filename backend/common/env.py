"""Local secret loader. One place that teaches backend/.env to the process.

The dev-container worker and the host API both read secrets from os.environ, but nothing
populates os.environ from backend/.env except the one-off loader scrape.py wrote for the
Apify key. This generalizes that: on import, fill in any key that backend/.env defines and
the environment doesn't already have.

Env-first is the whole contract. A value already in os.environ (set by the dev-container's
compose.yml, by Railway, or exported in the shell) always wins; .env only fills the gaps.
So DATABASE_URL / REDIS_URL / RATING_MODEL from compose are never clobbered, and prod (where
there's no .env file on disk) is a silent no-op. This is why importing it at the top of
common.db is safe: it runs before DATABASE_URL is read, but can't override it.

Deliberately a hand-rolled parser, not python-dotenv, to keep the dependency list honest and
match the raw-urllib / raw-SQL spirit of the rest of the backend. It handles KEY=VALUE, skips
blanks and # comments, and strips an optional surrounding pair of quotes.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_local_env() -> None:
    """Populate os.environ from the nearest backend/.env, without overriding what's set."""
    for base in [Path.cwd(), *Path(__file__).resolve().parents]:
        env_file = base / "backend" / ".env"
        if not env_file.exists():
            continue
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)  # env-first: never override an existing value
        return  # first backend/.env found wins, same as the Apify loader
