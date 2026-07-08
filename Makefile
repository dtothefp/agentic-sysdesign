DATABASE_URL ?= postgresql://lab:lab@localhost:5432/sysdesign
export DATABASE_URL

.PHONY: up schema partitions seed drills setup down reset

up:
	docker compose up -d db
	@echo "waiting for postgres..."
	@until docker compose exec -T db pg_isready -U lab -d sysdesign >/dev/null 2>&1; do sleep 1; done
	@echo "postgres ready on 5432"

schema:
	psql "$(DATABASE_URL)" -f common/schema.sql

partitions:
	psql "$(DATABASE_URL)" -f common/partitions.sql

seed:
	uv run python -m common.seed
	psql "$(DATABASE_URL)" -c "REFRESH MATERIALIZED VIEW daily_signal_rollup;"

drills:
	psql "$(DATABASE_URL)" -f drills/explain-drills.sql

setup: up schema partitions seed
	@echo "ready. run 'make drills', or open: psql \"$(DATABASE_URL)\""

down:
	docker compose down -v

reset: down setup
