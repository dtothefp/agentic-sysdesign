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

Two layers, complementary, both pointing at the same `docker-compose.yml`:

- `.devcontainer/`. Local reproducible container (VS Code or Cursor "Reopen in
  Container"). A Python workspace plus the Postgres service; `DATABASE_URL` inside the
  container points at `db:5432`.
- `.cursor/environment.json`. The config Cursor Cloud Agents read to boot their own
  environment. Installs `uv` and the Postgres client, syncs deps, and brings up the db.
  Cloud agents run in a Docker container on a VM. If docker-in-docker is not available
  in the agent environment, swap the `start` step for a Dockerfile-provisioned Postgres
  or an external database URL passed as a scoped secret.

## Git workflow

App tier. Feature branch plus PR, never commit to `main` directly. The scaffold itself
landed via PR to establish the pattern the Cloud Agents will follow.
