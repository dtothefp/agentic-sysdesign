# sysdesign

A system-design build guide, built as one real app: a scaled-down competitor-intelligence
pipeline that grows module by module. Each module is one interview competency, and building
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

```
backend/    all Python + database: common/, db/migrations/, drills/, api/, Makefile, pyproject.toml
frontend/   Next.js UI (empty until Module 2)
.claude/skills/   in-repo skills (e.g. scrape-signals)
.devcontainer/    reproducible container with its own sibling Postgres
```

Run all backend commands from `backend/`.

## Quick start (local)

Needs Docker, `uv`, `psql`, and `dbmate` on the host (`brew install dbmate`). The dev
container has all four preinstalled, so in Cursor you skip straight to `make db-init`.

```bash
cd backend
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
cd backend
make migrate    # apply every pending migration in order
make status     # show which have run, which are pending
make rollback   # undo the most recent migration (its migrate:down)
make new name=add_events_index   # scaffold the next timestamped migration
```

`backend/db/migrations/20260708000001_initial_schema.sql` creates the tables (`raw_signals`
RANGE partitioned by `captured_at`, the partition key inside every unique constraint, and a
durable `runs` job-of-record table that Module 2 writes to on state transitions while
pushing high-frequency progress to a Redis cache). `20260708000002_monthly_partitions.sql`
adds the monthly child partitions and an idempotent `create_month_partition(date)`
maintenance function (the production answer is `pg_partman`). These files are the single
source of truth for the schema shape.

## Drills

`backend/drills/explain-drills.sql` has six `EXPLAIN (ANALYZE, BUFFERS)` drills covering
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
  cd backend
  make db-init   # schema + partitions + seed, no `docker compose up` (db is already a sibling here)
  make drills
  ```

  Use `db-init` inside the container, not `make setup`. `setup` tries to `docker compose up`
  a db, which is the host workflow; inside the container the db is already running.
- `.cursor/environment.json`. The config Cursor Cloud Agents read to boot their own
  environment. Installs `uv` and the Postgres client, syncs deps in `backend/`, and brings
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
