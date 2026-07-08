DATABASE_URL ?= postgresql://lab:lab@localhost:5432/sysdesign
export DATABASE_URL

# dbmate reads DATABASE_URL; append sslmode=disable for the local container (no TLS).
# --no-dump-schema skips dbmate's schema.sql dump. The migration files are the source of
# truth, so there's no generated schema file to keep in sync.
DBMATE = dbmate --no-dump-schema --migrations-dir db/migrations --url "$(DATABASE_URL)?sslmode=disable"

.PHONY: up migrate rollback status new seed db-init drills setup down reset

up:
	docker compose up -d db
	@echo "waiting for postgres..."
	@until docker compose exec -T db pg_isready -U lab -d sysdesign >/dev/null 2>&1; do sleep 1; done
	@echo "postgres ready on 5432"

# apply every pending migration in order (db/migrations/*.sql).
migrate:
	@command -v dbmate >/dev/null || { echo "dbmate not found. Preinstalled in the dev container; on the host: brew install dbmate"; exit 1; }
	$(DBMATE) up

# undo the most recent migration (its migrate:down block).
rollback:
	$(DBMATE) down

# show which migrations have run and which are pending.
status:
	$(DBMATE) status

# scaffold a new timestamped migration: make new name=add_events_index
new:
	$(DBMATE) new $(name)

seed:
	uv run python -m common.seed
	psql "$(DATABASE_URL)" -c "REFRESH MATERIALIZED VIEW daily_signal_rollup;"

# migrate + seed against whatever DATABASE_URL points at, WITHOUT starting a container.
# Use this inside the dev container, where Postgres is already running as a sibling (db:5432).
# On the host, prefer `make setup`.
db-init: migrate seed
	@echo "db initialized at $(DATABASE_URL)"

drills:
	psql "$(DATABASE_URL)" -f drills/explain-drills.sql

# host one-shot: bring the db up (publishes 5432) then migrate + seed.
setup: up db-init
	@echo "ready. run 'make drills', or open: psql \"$(DATABASE_URL)\""

down:
	docker compose down -v

reset: down setup
