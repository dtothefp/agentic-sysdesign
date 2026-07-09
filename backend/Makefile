DATABASE_URL ?= postgresql://lab:lab@localhost:5432/sysdesign
export DATABASE_URL

# dbmate reads DATABASE_URL; append sslmode=disable for the local container (no TLS).
# --no-dump-schema skips dbmate's schema.sql dump. The migration files are the source of
# truth, so there's no generated schema file to keep in sync.
DBMATE = dbmate --no-dump-schema --migrations-dir db/migrations --url "$(DATABASE_URL)?sslmode=disable"

.PHONY: up migrate rollback status new seed db-init drills setup down reset api openapi scrape

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

# run the FastAPI surface with reload. docs at http://localhost:8000/docs
api:
	uv run uvicorn api.main:app --reload --host 0.0.0.0 --port 8000

# scrape every watchlist influencer's recent Instagram posts through the API (Apify REST).
# needs the API running (make api) and APIFY_API_KEY in backend/.env.
scrape:
	uv run python ../.claude/skills/scrape-signals/scrape_ig.py

# export the OpenAPI spec to a file the Module 2 frontend codegens a typed client from.
# app.openapi() only introspects the routes, so this needs no running db or server.
openapi:
	uv run python -c "import json; from api.main import app; print(json.dumps(app.openapi(), indent=2))" > openapi.json
	@echo "wrote backend/openapi.json"

# host one-shot: bring the db up (publishes 5432) then migrate + seed.
setup: up db-init
	@echo "ready. run 'make drills', or open: psql \"$(DATABASE_URL)\""

down:
	docker compose down -v

reset: down setup
