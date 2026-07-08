# sysdesign-m1-data-model

Module 1 of the system-design build guide. A partitioned Postgres schema for a
scaled-down competitor-intelligence pipeline, plus the EXPLAIN drills that make the
partitioning and indexing choices defensible out loud in an interview.

This is throwaway learning code, not a product. The point is to have run it, broken
it, and read the query plans, so a schema and data-architecture round has nothing in
it you have not already touched with your hands.

## What it builds

- `common/schema.sql`. The locked schema every later module reuses. `raw_signals` is
  RANGE partitioned by `captured_at`, the partition key lives inside every unique
  constraint, and there is a durable `runs` job-of-record table (Module 2 writes to
  it on state transitions while pushing high-frequency progress to a Redis cache).
- `common/partitions.sql`. Monthly child partitions plus an idempotent
  `create_month_partition(date)` maintenance function (the production answer is
  `pg_partman`).
- `common/seed.py`. Loads 5 competitors and ~4000 signals across three months so
  partition pruning has something to prune. Idempotent on the locked unique key, so
  re-running inserts nothing.
- `drills/explain-drills.sql`. Six `EXPLAIN (ANALYZE, BUFFERS)` drills covering
  pruning, index vs seq scan, the no-partition-key anti-pattern, covering index-only
  scans, the matview read/write split, and concurrent refresh.

## Quick start (local)

Needs Docker, `uv`, and a `psql` client on the host.

```bash
make setup     # up + schema + partitions + seed, one shot
make drills    # run the six EXPLAIN drills
# or open a shell:
psql "postgresql://lab:lab@localhost:5432/sysdesign"
```

`make down` drops the volume for a clean slate. `make reset` is down then setup.

## Dev environments

Two layers, complementary:

- `.devcontainer/`. Local reproducible container (VS Code or Cursor "Reopen in
  Container"). It's self-contained: a Python workspace plus its own Postgres, defined in
  `.devcontainer/compose.yml`. The workspace reaches the db over the internal network at
  `db:5432`, so the container's Postgres does NOT publish a host port. That's deliberate.
  It means the dev container never collides with a host-run `make up` on 5432, and you
  can run both at once. The tradeoff is the container's db is a separate volume, so it
  starts empty. Seed it once from a container terminal:

  ```bash
  make db-init   # schema + partitions + seed, no `docker compose up` (db is already a sibling here)
  make drills
  ```

  Use `db-init` inside the container, not `make setup`. `setup` tries to `docker compose
  up` a db, which is the host workflow; inside the container the db is already running.
- `.cursor/environment.json`. The config Cursor Cloud Agents read to boot their own
  environment. Installs `uv` and the Postgres client, syncs deps, and brings up the db.
  Cloud agents run in a Docker container on a VM. If docker-in-docker is not available
  in the agent environment, swap the `start` step for a Dockerfile-provisioned Postgres
  or an external database URL passed as a scoped secret.

### If the dev container fails with "port is already allocated"

That means something on the host already holds `5432` (usually a `make setup` / `make up`
you ran earlier). The current dev container no longer publishes 5432, so a fresh
"Rebuild Container" resolves it. If you're on an older checkout that still published the
port, either stop the host stack first (`make down`) or pull this fix.

## Git workflow

App tier. Feature branch plus PR, never commit to `main` directly. The scaffold itself
landed via PR to establish the pattern the Cloud Agents will follow.
