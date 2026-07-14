# sysdesign

A system-design build guide, built as one real app: a scaled-down influencer-intelligence
pipeline (Defrag's creator watchlist) that grows module by module. Each module is one
interview competency, and building
it is the prep. This is throwaway learning code, not a product. The point is to have run
it, broken it, and read the query plans, so a backend round has nothing in it you haven't
already touched with your hands.

## Module ladder

The five modules are cumulative layers of one app, not separate apps. They live in one repo
that accumulates; each finished module gets a git tag (`module-1`, `module-2`, ...) so any
milestone is one `git checkout` away.

1. **Data model** (current). Partitioned Postgres schema, per-partition indexes, a
   read-path materialized view, EXPLAIN drills, and a FastAPI surface over it.
2. **Celery fan-out.** Background scraping jobs writing to a durable `runs` table, live
   progress in Redis, a Next.js frontend polling it.
3. **AWS-native.** Deploy it for real.
4. **pgvector.** Semantic search over the signals.
5. **LLM + graph.** Summarize signals into events and digests, build relationships.

## Layout

A moon + uv-workspace monorepo. One `uv.lock` at the root covers every member; moon runs
per-project tasks (`moon run api:dev`), and the root Makefile keeps the db lifecycle and
secret-bearing agent targets.

```
.moon/            moon workspace config (also the .env loader's repo-root marker)
apps/chat-web/    React chat agent UI (scaffold, Module 7)
services/api/     FastAPI surface (sysdesign-api) + its railway.json + tests
services/worker/  Celery worker + beat (sysdesign-worker) + its railway.json
packages/core/    shared common/ + db/ migrations + drills + tests (sysdesign-core)
packages/task-contract/  task names + send-only Celery client (decouples api from worker)
packages/agents/  Module 5 Managed Agents YAML + agentctl.py
infra/            Railway env-var sync + preview-env scripts
.claude/skills/   in-repo skills (e.g. scrape-signals)
.devcontainer/    reproducible container with its own sibling Postgres
```

Run everything from the repo root. `uv sync --all-packages` installs the whole workspace.

## Quick start (local)

Needs Docker, `uv`, `psql`, and `dbmate` on the host (`brew install dbmate`). The dev
container has all four preinstalled, so in Cursor you skip straight to `make db-init`.

```bash
make setup     # up + migrate + seed, one shot
make drills    # run the six EXPLAIN drills (whole file, smoke test)
# or open an interactive shell to study one plan at a time:
psql "postgresql://lab:lab@localhost:5432/sysdesign"
```

`make down` drops the volume for a clean slate. `make reset` is down then setup.

## Migrations (dbmate)

A migration is just an ordered SQL file with a `migrate:up` block (apply) and a
`migrate:down` block (undo). dbmate records which files have run in a `schema_migrations`
table, so it never runs one twice. No ORM, no codegen.

```bash
make migrate    # apply every pending migration in order
make status     # show which have run, which are pending
make rollback   # undo the most recent migration (its migrate:down)
make new name=add_events_index   # scaffold the next timestamped migration
```

`packages/core/db/migrations/20260708000001_initial_schema.sql` creates the tables (`raw_signals`
RANGE partitioned by `captured_at`, the partition key inside every unique constraint, and a
durable `runs` job-of-record table that Module 2 writes to on state transitions while
pushing high-frequency progress to a Redis cache). `20260708000002_monthly_partitions.sql`
adds the monthly child partitions and an idempotent `create_month_partition(date)`
maintenance function (the production answer is `pg_partman`).
`20260708000003_influencer_signals_schema.sql` reframes the domain from generic competitors
to Defrag's influencer watchlist: it renames `competitors` to `influencers`, renames
`competitor_id` to `influencer_id` everywhere (one ALTER cascades across all raw_signals
partitions), adds `instagram_handle` and the `last_scraped_at` scrape watermark, and rebuilds
the rollup matview. All renames, no data dropped, so you can read exactly what moved.
`20260708000004_older_partitions.sql` backfills partition coverage for Jan through Apr 2026
(the initial partitions start at 2026-05-01), so real posts from earlier in the year have a
child partition to land in. These files are the single source of truth for the schema shape.

A full write-up of Module 1 (what's in it, why each choice, and a phased test walkthrough
covering the migrations, the API, the scrape skill, and the EXPLAIN drills) lives in
[`docs/module-1.md`](docs/module-1.md).

## API

A thin FastAPI surface over the schema lives in `services/api/`. Run it from the repo root:

```bash
make api    # uvicorn at http://localhost:8000, interactive docs at /docs
```

Endpoints: `/influencers` (`POST` a single creator or `POST /influencers/bulk` for the whole
watchlist, both upsert on `instagram_handle`), `/sources`, `/signals`, `/rollup`. Two things
it's built to show. `POST /signals` is the idempotent `ON CONFLICT DO NOTHING` upsert with
`content_hash` derived server-side, so re-POSTing the identical signal is a no-op. `GET /signals`
requires a `from`/`to` window, so every read carries the partition key and prunes to the
relevant month(s) instead of fanning across all partitions. `PATCH /influencers/{id}` advances
the `last_scraped_at` watermark the incremental scraper reads.

To fill the database with real (not synthetic) data, use the in-repo `scrape-signals` skill
(`.claude/skills/scrape-signals/`), which is how Claude Code drives the database. It's a loop
entirely over the API: `POST /influencers/bulk` to seed the watchlist, `GET /influencers` to
read them back, scrape each one's recent Instagram posts (Apify REST, all in parallel,
incremental off each watermark), then `POST /signals` for each post. Every write takes the same
idempotent path the app uses. Needs `APIFY_API_KEY` in the repo-root `.env` (gitignored). There's no
`make scrape`; the scrape is a skill Claude Code runs, not a build target.

### OpenAPI

FastAPI generates the OpenAPI spec automatically from the Pydantic models and route
signatures. No extra library. While `make api` is running, the spec is machine-readable at
`/openapi.json`, with Swagger UI at `/docs` and ReDoc at `/redoc`. `operationId`s are the
handler names (`list_signals`, `create_signal`) so a generated client reads cleanly.

`make openapi` writes the spec to `services/api/openapi.json` without a running db or server (it
only introspects the routes). That file is the codegen input for the Module 2 Next.js
frontend, which points a typed-client generator (openapi-typescript / orval) at it to get a
fully typed API client. Regenerate it whenever the API surface changes.

## Drills

`packages/core/drills/explain-drills.sql` has six `EXPLAIN (ANALYZE, BUFFERS)` drills covering
pruning, index vs seq scan, the no-partition-key anti-pattern, covering index-only scans,
the matview read/write split, and concurrent refresh. Run them one block at a time in an
interactive `psql` session to read each plan on its own; `make drills` fires all six at
once and is only useful as a smoke test.

## Dev environments

- `.devcontainer/`. Local reproducible container (VS Code or Cursor "Reopen in Container").
  Self-contained: a Python workspace plus its own Postgres, defined in
  `.devcontainer/compose.yml`. The workspace reaches the db over the internal network at
  `db:5432`, so the container's Postgres does NOT publish a host port. That's deliberate. It
  means the dev container never collides with a host-run `make up` on 5432, and you can run
  both at once. The tradeoff is the container's db is a separate volume, so it starts empty.
  Seed it once from a container terminal:

  ```bash
  make db-init   # schema + partitions + full seed, no `docker compose up` (db is a sibling here)
  make drills
  ```

  Use `db-init` inside the container, not `make setup`. `setup` tries to `docker compose up`
  a db, which is the host workflow; inside the container the db is already running.

  `db-init` runs the *full* seed (the watchlist plus 4000 synthetic signals, so the drills have
  volume). If you just want a clean database with only the influencers and none of the synthetic
  rows, run `make db-fresh` instead: it drops the db, re-applies every migration from empty, and
  seeds only the watchlist. Stop `make api` first, since an open connection blocks the drop.
  `make seed-influencers` seeds just the watchlist without touching the schema.
- `.cursor/environment.json`. The config Cursor Cloud Agents read to boot their own
  environment. Installs `uv` and the Postgres client, syncs the workspace (`uv sync --all-packages`), and brings
  up the db. Cloud agents run in a Docker container on a VM. If docker-in-docker is not
  available in the agent environment, swap the `start` step for a Dockerfile-provisioned
  Postgres or an external database URL passed as a scoped secret.

### If the dev container fails with "port is already allocated"

Something on the host already holds `5432` (usually a `make setup` / `make up` you ran
earlier). The current dev container no longer publishes 5432, so a fresh "Rebuild
Container" resolves it. If you're on an older checkout that still published the port, either
stop the host stack first (`make down`) or pull this fix.

## Git workflow

App tier. Feature branch plus PR, never commit to `main` directly. Tag each completed module
on `main`.
