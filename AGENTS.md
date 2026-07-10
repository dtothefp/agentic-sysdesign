# sysdesign: Agent Instructions

> `CLAUDE.md` and `GEMINI.md` are symlinks to this file.

One repo for the whole system-design build guide, a scaled-down influencer-intelligence
pipeline (Defrag's creator watchlist) built up in modules. Personal interview-prep learning
code, internal, never client-facing.

The modules are cumulative layers of a single app, not separate apps, so they live
in one repo that accumulates. Each finished module gets a git tag (`module-1`,
`module-2`, ...) so any milestone is one `git checkout` away.

- **Module 1, data model.** Partitioned Postgres schema, per-partition indexes, a
  read-path materialized view, EXPLAIN drills, and the FastAPI surface over it.
- **Module 2, Celery fan-out.** `POST /runs` fans out one Celery task per influencer (a
  chord), each writing signals through the same `common.signals.insert_signal` upsert the API
  uses, bumping the `runs` row's `done_count`, and publishing a progress delta to a Redis
  channel. The frontend streams progress over **Server-Sent Events** (`GET /runs/{id}/stream`,
  `sse-starlette`), not polling: the endpoint reads the `runs` snapshot from Postgres on
  connect (so a page refresh re-establishes state) then subscribes to Redis for live deltas.
  The chord's fan-in callback runs `REFRESH MATERIALIZED VIEW CONCURRENTLY daily_signal_rollup`
  so the rollup reflects each run the moment it finishes, with a Celery-beat task (every 5 min)
  as a staleness backstop. This is why Module 1 built the matview with a unique index on
  `(influencer_id, day)`, that index is what lets `CONCURRENTLY` refresh without locking
  dashboard readers. Runs have a `demo` mode (synthetic signals, no Apify spend) for watching
  the fan-out and SSE stream cheaply, and a `live` mode (real Apify scrape). Backend lives in
  `backend/worker/` (`celery_app.py`, `tasks.py`, `scrape.py`); run it with `make worker`.
  Full first-principles walkthrough (Redis's two hats, chord = distributed Promise.all,
  snapshot-then-deltas SSE, debugging with `--pool solo`) in [docs/module-2.md](docs/module-2.md).
- **Module 3, deploy (SHIPPED).** Railway for the API + worker + Redis (next to the
  existing shared AI service), Postgres on Supabase with pgvector enabled. Live at
  sysdesign.thedefrag.ai; the infra contract, gotchas, and migration story are in
  [infra/README.md](infra/README.md). Migrations run automatically as the api's
  `preDeployCommand` (a deploy can't ship code ahead of its schema). A planned piece,
  moving the beat backstop to a Supabase Edge Function on pg_cron to learn the serverless
  constraints (stateless, wall-clock limits, chunked work re-entered via a durable run
  row), is DEFERRED until the Supabase single-project consolidation decides which project
  the function lives in (see `packages/package-supabase/`). The lesson stands either way,
  long unreliable work stays on the queue-backed worker, short scheduled work goes
  serverless.
- **Module 4, AI rating layer.** An LLM lands in the write path as a new pipeline stage,
  not a longer task. Each scrape task already knows which signals it newly inserted
  (`insert_signal` returns `inserted`), so it enqueues one `rate_signal` Celery job per new
  row and finishes; ratings drain through the same worker pool with their own retries, and
  a slow model call never blocks a scrape. The rating is a single structured-output Claude
  call (relevance to Defrag's AI-research thesis, topics, summary, confidence) written
  idempotently keyed on the signal's `content_hash`, deduping on the INPUT hash rather than
  the model's answer because the model is non-deterministic. A semantic cache (pgvector
  nearest-neighbor over embedded captions) serves the prior rating when cosine similarity
  clears a threshold, skipping the model entirely. A beat sweep for unrated signals is the
  backstop, same pattern as the matview refresh. The AWS rebuild that once held this module
  number was cut to a read-only talk track (parent package, `notes/aws-talk-track.md`); the
  concepts were already built here in Celery and the vocabulary mapping is readable.
- **Module 5, Managed Agent capstone.** A scheduled Anthropic Managed Agent (the digest
  bot) runs the daily sweep, pulls the week's rated signals through the deployed API (the
  sandbox reaches Railway over HTTP; start with bash + the OpenAPI spec, graduate to an MCP
  wrapper), clusters themes, compares against last week via a Memory Store mounted at
  `/mnt/memory/`, and writes `digests` rows. Pipeline for volume, agent for judgment; the
  per-signal ratings from Module 4 are what the agent reasons over, and the API is its tool
  surface. Requires the Module 3 deploy, already live (the sandbox can't reach localhost).
- **Module 6, hybrid search (stretch).** pgvector HNSW + Postgres full-text + Reciprocal
  Rank Fusion over signal content. Upgrades the API's search and becomes a retrieval tool
  the digest agent can call.

## Layout

Standard `backend/` + `frontend/` split, matching ExtractIQ and SignalDashboard and the
app-setup skill.

```
backend/    all Python + database: common/, db/migrations/, drills/, api/, Makefile, pyproject.toml
frontend/   Next.js UI, empty until Module 2
.claude/skills/   in-repo skills (e.g. scrape-signals), tracked branch-by-branch with the code they drive
.devcontainer/    reproducible container (its own sibling Postgres at db:5432)
```

Run all backend commands from `backend/`.

## Stack

Postgres 16 with pgvector (Docker), Python 3.13 managed by `uv`, `psql` for the drills,
`dbmate` for migrations, FastAPI for the API surface. The frontend (Next.js) arrives in
Module 2 and talks to the FastAPI backend over HTTP.

## Build / Dev

All from `backend/`:

```bash
cd backend
make setup       # HOST: docker compose up db, then migrate + full seed
make db-init     # DEV CONTAINER: migrate + full seed (influencers + 4000 drill signals)
make db-fresh    # DEV CONTAINER: drop db, re-migrate from empty, seed ONLY influencers (no signals)
make db-empty    # DEV CONTAINER: drop db, re-migrate from empty, seed NOTHING (skill adds influencers via API)
make migrate     # apply pending dbmate migrations (db/migrations/*.sql)
make status      # which migrations have run vs pending
make rollback    # undo the most recent migration (its migrate:down)
make new name=X  # scaffold the next timestamped migration
make seed        # full seed: watchlist + 4000 synthetic signals (drill volume)
make seed-influencers  # just the watchlist, no signals
make drills      # run drills/explain-drills.sql (whole file, smoke test)
make api         # run the FastAPI surface (uvicorn, reload) at :8000, docs at /docs
make worker      # DEV CONTAINER: Celery worker for Module 2 fan-out jobs (needs Redis + the API)
make worker-beat # DEV CONTAINER: Celery beat, periodic rollup-refresh backstop (optional, alongside worker)
make openapi     # export the OpenAPI spec to backend/openapi.json (no db/server needed)
make down        # drop the volume
make reset       # down then setup
uv run python -m common.seed   # re-seed (idempotent, inserts nothing the second time)
```

The FastAPI app lives in `backend/api/` (`api.main:app`). Endpoints: `/influencers` (POST a
single creator, POST `/influencers/bulk` for the whole watchlist, both upsert on
instagram_handle; PATCH advances the last_scraped_at watermark), `/sources`, `/signals` (POST
is the idempotent `ON CONFLICT` upsert, content_hash derived server-side; GET requires a
`from`/`to` window so it always prunes), and `/rollup` (reads the matview). To populate real
data, use the in-repo `scrape-signals` skill (`.claude/skills/scrape-signals/`), which is how
Claude Code drives the database. It's a loop over the API: POST the watchlist, GET it back,
scrape each creator's recent IG posts (Apify REST, in parallel, incremental off each
watermark), then POST each post to `/signals`. There's no `make scrape`; it's a skill Claude
Code runs, not a build target. Needs `APIFY_API_KEY` in `backend/.env` (gitignored, never commit it).

FastAPI generates the OpenAPI spec automatically (`/openapi.json`, Swagger at `/docs`,
ReDoc at `/redoc`); no separate OpenAPI library. `operationId`s are the handler names
(`list_signals`, `create_signal`) so generated clients read cleanly. `make openapi` dumps the spec to `backend/openapi.json`
(introspection only, no db/server), which the Module 2 Next.js frontend codegens a typed
client from. Keep raw SQL via psycopg, no ORM. The explicit SQL is the studyable artifact
(partition pruning, ON CONFLICT, index usage stay visible), and it matches the dbmate
raw-SQL migration choice. A later tag revisits the data-access layer with SQLAlchemy as a
deliberate before/after (same endpoints, ORM instead of raw SQL); Module 1 stays raw SQL.

To study a single query plan, don't use `make drills` (it fires the whole file at once).
Open an interactive shell with `psql "$DATABASE_URL"` and paste one drill block at a time.

`DATABASE_URL` defaults to `postgresql://lab:lab@localhost:5432/sysdesign` on the host,
and `db:5432` inside the dev container. dbmate reads it with `?sslmode=disable` appended
for the local no-TLS container.

The dev container (`.devcontainer/compose.yml`) is standalone and runs its own Postgres
with no host port publish, so it never collides with a host-run `make up` on 5432. Its db
is a separate volume, so seed it once with `make db-init` (from `backend/`) in a container
terminal. Do not add `include: ../docker-compose.yml` back. Compose appends port mappings,
which reintroduces the 5432 collision.

## Git workflow

App tier. ALWAYS a feature branch plus PR. Never commit to `main` directly. Tag each
completed module on `main` (`git tag module-1 && git push origin module-1`). Schema
changes go through a new dbmate migration in `backend/db/migrations/` (`make new
name=...`), never by editing an already-applied migration file. Add the `migrate:down`
block too, so rollback works.

## The one idempotency sentence

At-least-once delivery is assumed everywhere. Every write is an idempotent upsert keyed
on `(influencer_id, content_hash, captured_at)` via `INSERT ... ON CONFLICT DO NOTHING`,
so reprocessing the same item twice is a no-op. That answers most "what if it runs
twice" and "how do you avoid duplicates" probes.

## Writing style

No em dashes or en dashes as punctuation. Use commas, parens, periods. No prose colons
introducing a list in body text. Use contractions.
