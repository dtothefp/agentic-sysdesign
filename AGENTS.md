# sysdesign-m1-data-model: Agent Instructions

> `CLAUDE.md` and `GEMINI.md` are symlinks to this file.

App repo for Module 1 of the system-design build guide. A partitioned Postgres schema
plus EXPLAIN drills for a scaled-down competitor-intelligence pipeline. Personal
interview-prep learning code, internal, never client-facing.

Part of a per-module family. Later modules (Celery fan-out, AWS-native, pgvector,
LLM, graph) each get their own `sysdesign-m*` repo that reuses this same `common/`
schema. Keep the schema here the source of truth for the shape.

## Stack

Postgres 16 with pgvector (Docker), Python 3.13 managed by `uv`, `psql` for the drills,
`dbmate` for migrations. No web framework and no frontend in this module. Module 1 is pure
schema and query-plan work, so a frontend would be idle. The frontend arrives in the
Module 2 repo where the `runs` and progress-polling pattern is actually exercised.

## Build / Dev

```bash
make setup       # HOST: docker compose up db, then migrate + seed
make db-init     # DEV CONTAINER: migrate + seed, no `docker compose up` (db is a sibling)
make migrate     # apply pending dbmate migrations (db/migrations/*.sql)
make status      # which migrations have run vs pending
make rollback    # undo the most recent migration (its migrate:down)
make new name=X  # scaffold the next timestamped migration
make drills      # run drills/explain-drills.sql
make down        # drop the volume
make reset       # down then setup
uv run python -m common.seed   # re-seed (idempotent, inserts nothing the second time)
```

`DATABASE_URL` defaults to `postgresql://lab:lab@localhost:5432/sysdesign` on the host,
and `db:5432` inside the dev container. dbmate reads it with `?sslmode=disable` appended
for the local no-TLS container.

The dev container (`.devcontainer/compose.yml`) is standalone and runs its own Postgres
with no host port publish, so it never collides with a host-run `make up` on 5432. Its db
is a separate volume, so seed it once with `make db-init` from a container terminal. Do
not add `include: ../docker-compose.yml` back. Compose appends port mappings, which
reintroduces the 5432 collision.

## Git workflow

App tier. ALWAYS a feature branch plus PR. Never commit to `main` directly. Schema changes
go through a new dbmate migration in `db/migrations/` (`make new name=...`), never by
editing an already-applied migration file. Add the `migrate:down` block too, so rollback
works.

## The one idempotency sentence

At-least-once delivery is assumed everywhere. Every write is an idempotent upsert keyed
on `(competitor_id, content_hash, captured_at)` via `INSERT ... ON CONFLICT DO NOTHING`,
so reprocessing the same item twice is a no-op. That answers most "what if it runs
twice" and "how do you avoid duplicates" probes.

## Writing style

No em dashes or en dashes as punctuation. Use commas, parens, periods. No prose colons
introducing a list in body text. Use contractions.
