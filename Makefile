# NuroFlow developer commands. Run `make` or `make help` for the list.
SHELL        := /bin/bash
.DEFAULT_GOAL := help
COMPOSE_DEV  := docker compose -f docker-compose.dev.yml
BACKEND      := cd backend &&
RUN          := $(BACKEND) uv run

.PHONY: help install up down nuke logs migrate migration downgrade sql db-shell api worker scheduler admin \
        test test-unit test-integration lint format typecheck check

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---- setup ----------------------------------------------------------------- #
install: ## Install Python deps (incl. dev) and git hooks
	$(BACKEND) uv sync
	$(RUN) pre-commit install
	@test -f backend/.env || cp .env.example backend/.env && echo "backend/.env ready"

# ---- infrastructure -------------------------------------------------------- #
up: ## Start Postgres + Redis and wait until healthy
	$(COMPOSE_DEV) up -d --wait

down: ## Stop dev services (keeps data)
	$(COMPOSE_DEV) down

nuke: ## Stop dev services and DELETE all local data
	$(COMPOSE_DEV) down -v

logs: ## Tail dev service logs
	$(COMPOSE_DEV) logs -f

db-shell: ## Open psql in the dev database
	$(COMPOSE_DEV) exec postgres psql -U nuroflow -d nuroflow

# ---- database migrations --------------------------------------------------- #
migrate: ## Apply all migrations
	$(RUN) alembic upgrade head

migration: ## Create a migration: make migration m="add foo to bar"
	@test -n "$(m)" || (echo 'usage: make migration m="message"' && exit 1)
	$(RUN) alembic revision --autogenerate -m "$(m)"

downgrade: ## Roll back one migration
	$(RUN) alembic downgrade -1

sql: ## Print the SQL for all migrations (offline mode) for review
	$(RUN) alembic upgrade head --sql

# ---- run ------------------------------------------------------------------- #
api: ## Run the API with auto-reload on http://127.0.0.1:8000 (docs at /docs)
	$(RUN) nuroflow-api --reload

worker: ## Run a Celery worker on all queues (default, ai, notifications)
	$(RUN) nuroflow-worker

scheduler: ## Run the scheduler (cron, retries, reaper, timeouts, admission)
	$(RUN) nuroflow-scheduler

admin: ## Create or promote an admin: make admin email=you@example.com
	@test -n "$(email)" || (echo 'usage: make admin email=you@example.com' && exit 1)
	$(RUN) nuroflow create-admin --email "$(email)"

# ---- quality ---------------------------------------------------------------- #
test: ## Run all tests with coverage (integration tests need `make up`)
	$(RUN) pytest --cov --cov-report=term-missing

test-unit: ## Run fast unit tests only
	$(RUN) pytest tests/unit

test-integration: ## Run integration tests against Postgres
	$(RUN) pytest tests/integration -m integration

lint: ## Lint with Ruff
	$(RUN) ruff check .

format: ## Auto-format and auto-fix with Ruff
	$(RUN) ruff format . && $(RUN) ruff check --fix .

typecheck: ## Type-check with mypy (strict)
	$(RUN) mypy

check: lint typecheck test ## Everything CI runs
	$(RUN) alembic check
