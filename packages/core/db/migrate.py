"""Apply dbmate-format migrations to the database in DATABASE_URL.

Runs as Railway's `preDeployCommand`, so a deploy never ships code ahead of its schema. It
reads the same `db/migrations/*.sql` files dbmate uses locally, tracks applied versions in
the same `schema_migrations` table, and is idempotent, already-applied versions are skipped.

Why this exists instead of dbmate in prod. dbmate is a Go binary and the Railway image is a
uv/Python (Railpack) build with no dbmate in it. Rather than wrangle a binary into the image,
this reads dbmate's file format faithfully with psycopg (already a dependency). dbmate stays
the local dev tool (`make migrate`), this is the prod applier, and both agree because they
share the files and the `schema_migrations` table.

dbmate file format, everything before `-- migrate:up` is header comments, the up SQL runs
between `-- migrate:up` and `-- migrate:down`, and a `transaction:false` suffix on the
`-- migrate:up` line opts a migration out of the wrapping transaction (for `CREATE INDEX
CONCURRENTLY` and friends). A `transaction:false` migration must be a single statement so
Postgres doesn't wrap it in an implicit transaction block.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def parse_up(text: str) -> tuple[str, bool]:
    """Return (up_sql, in_transaction) for one dbmate migration file."""
    lines = text.splitlines()
    up_start: int | None = None
    in_txn = True
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("-- migrate:up"):
            up_start = i + 1
            in_txn = "transaction:false" not in s
        elif s.startswith("-- migrate:down"):
            if up_start is None:
                break
            return "\n".join(lines[up_start:i]).strip(), in_txn
    if up_start is None:
        raise ValueError("no '-- migrate:up' marker")
    return "\n".join(lines[up_start:]).strip(), in_txn


def version_of(path: Path) -> str:
    """dbmate's version is the timestamp prefix before the first underscore."""
    return path.name.split("_", 1)[0]


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("migrate: DATABASE_URL not set", file=sys.stderr)
        return 1

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print("migrate: no migration files found")
        return 0

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version varchar(255) PRIMARY KEY)")
        applied = {v for (v,) in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        pending = [f for f in files if version_of(f) not in applied]

        if not pending:
            print(f"migrate: up to date ({len(applied)} applied, 0 pending)")
            return 0

        for f in pending:
            version = version_of(f)
            up_sql, in_txn = parse_up(f.read_text())
            print(f"migrate: applying {f.name} (transaction={in_txn})")
            if in_txn:
                with conn.transaction():
                    conn.execute(up_sql)
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            else:
                conn.execute(up_sql)
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))

        print(f"migrate: applied {len(pending)} migration(s), {len(applied) + len(pending)} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
