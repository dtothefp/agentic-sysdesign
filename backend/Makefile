DATABASE_URL ?= postgresql://lab:lab@localhost:5432/sysdesign
export DATABASE_URL

# dbmate reads DATABASE_URL; append sslmode=disable for the local container (no TLS).
# --no-dump-schema skips dbmate's schema.sql dump. The migration files are the source of
# truth, so there's no generated schema file to keep in sync.
DBMATE = dbmate --no-dump-schema --migrations-dir db/migrations --url "$(DATABASE_URL)?sslmode=disable"

.PHONY: up migrate rollback status new seed seed-influencers db-init db-fresh db-empty drills setup down reset api openapi worker worker-beat ollama-pull lint format test agent-deploy agent-run agent-list

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

# full seed: the watchlist AND 4000 synthetic signals, so the EXPLAIN drills have volume.
seed:
	uv run python -m common.seed
	psql "$(DATABASE_URL)" -c "REFRESH MATERIALIZED VIEW daily_signal_rollup;"

# seed ONLY the watchlist influencers (from the skill's watchlist.json). No signal volume.
seed-influencers:
	uv run python -m common.seed --influencers-only

# migrate + full seed (influencers + drill volume) against whatever DATABASE_URL points at,
# WITHOUT starting a container. Use inside the dev container (Postgres is a sibling at db:5432).
# On the host, prefer `make setup`. Use `make db-fresh` if you want a clean db with just the
# influencers and none of the synthetic signals.
db-init: migrate seed
	@echo "db initialized at $(DATABASE_URL)"

# DEV CONTAINER clean slate: drop the whole db, re-apply every migration from empty, then seed
# ONLY the influencers (no synthetic signals, no leftover rows). This is the "new database, just
# the watchlist" path. Stop `make api` first, since an open connection blocks DROP DATABASE.
db-fresh:
	$(DBMATE) drop || true
	$(DBMATE) up
	uv run python -m common.seed --influencers-only
	@echo "fresh db at $(DATABASE_URL): migrations applied, influencers seeded, no signals. run 'make seed' later if you want drill volume."

# DEV CONTAINER truly empty: drop the db and re-apply every migration, but seed NOTHING. Use
# this when you want the scrape-signals skill to add the influencers through the API instead of
# the Python seed. Stop `make api` first (an open connection blocks DROP DATABASE).
db-empty:
	$(DBMATE) drop || true
	$(DBMATE) up
	@echo "empty db at $(DATABASE_URL): migrations applied, no rows. add influencers via the scrape-signals skill."

drills:
	psql "$(DATABASE_URL)" -f drills/explain-drills.sql

# run the FastAPI surface with reload. docs at http://localhost:8000/docs
api:
	uv run uvicorn api.main:app --reload --host 0.0.0.0 --port 8000

# Module 2: the Celery worker that runs fan-out scrape jobs. Needs Redis (the dev container
# provides it) and DATABASE_URL. Run this in its own dev-container terminal alongside `make api`,
# then POST /runs to fan out. --concurrency 4 lets four influencers scrape in parallel.
worker:
	uv run celery -A worker.celery_app worker --loglevel INFO --concurrency 4

# Optional periodic backstop that re-refreshes the rollup every 5 min (celery beat is a
# separate singleton process). The fan-in already refreshes after each run, so this is belt-
# and-suspenders. Run alongside `make worker`.
worker-beat:
	uv run celery -A worker.celery_app beat --loglevel INFO

# DEV CONTAINER: pull the Module 4 local rating model into the ollama sibling service.
# One-time (the model persists in the `ollama` compose volume). Uses Ollama's native
# /api/pull because the OpenAI-compat layer only covers inference, not model management.
# llama3.2:1b on purpose. It's small and non-thinking, so it answers well inside the
# adapter's 180s timeout on container CPU. qwen3:4b (the original pick) thinks for ~700
# tokens per rating and never beats the deadline, see .devcontainer/compose.yml.
ollama-pull:
	curl -sf http://ollama:11434/api/pull -d '{"model": "llama3.2:1b"}'
	@echo "pulled llama3.2:1b"

# export the OpenAPI spec to a file the Module 2 frontend codegens a typed client from.
# app.openapi() only introspects the routes, so this needs no running db or server.
openapi:
	uv run python -c "import json; from api.main import app; print(json.dumps(app.openapi(), indent=2))" > openapi.json
	@echo "wrote backend/openapi.json"

# lint the backend with ruff (pyflakes + pycodestyle + import sort + pyupgrade + bugbear)
# and check formatting. Both are enforced in CI (.github/workflows/ci.yml); run `make format`
# to auto-apply the formatter before pushing.
lint:
	uv run ruff check .
	uv run ruff format --check .

# auto-apply ruff's formatter across the backend (the writing counterpart to `make lint`).
format:
	uv run ruff format .

# run the test suite (pytest). Unit tests are hermetic; the tests marked `integration` need a
# live Postgres (make setup / db-init) and auto-skip when it's unreachable, so this is safe to
# run anywhere. Pass EXTRA=-k... or EXTRA='-m "not integration"' to filter.
test:
	uv run pytest $(EXTRA)

# host one-shot: bring the db up (publishes 5432) then migrate + seed.
setup: up db-init
	@echo "ready. run 'make drills', or open: psql \"$(DATABASE_URL)\""

down:
	docker compose down -v

reset: down setup

# Module 5: deploy/run the digest agent via m5_agents/agentctl.py (a thin `ant`
# CLI wrapper). Needs ANTHROPIC_API_KEY (for `ant`) and SYSDESIGN_API_KEY (for
# the vault credentials) in the environment; agentctl reads shared resource ids
# from m5_agents/resources.json. CI drives the same script (see the app repo's
# .github/workflows/agent-deploy.yml); these targets are the local hand-crank.
#
#   make agent-deploy TIER=local BRANCH=$(git branch --show-current) RUN=1
#   make agent-run    TIER=local BRANCH=$(git branch --show-current)
#   make agent-list
TIER ?= local
BRANCH ?= $(shell git branch --show-current)
agent-deploy:
	uv run python m5_agents/agentctl.py deploy --tier $(TIER) \
	  $(if $(BRANCH),--branch $(BRANCH),) $(if $(PR),--pr $(PR),) \
	  $(if $(BASE_URL),--base-url $(BASE_URL),) $(if $(RUN),--run,)

agent-run:
	uv run python m5_agents/agentctl.py run --tier $(TIER) \
	  $(if $(BRANCH),--branch $(BRANCH),) $(if $(PR),--pr $(PR),)

agent-list:
	uv run python m5_agents/agentctl.py list
