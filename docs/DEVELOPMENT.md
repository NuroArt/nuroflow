# Development Guide

## Prerequisites

| Tool | Version | Install |
|---|---|---|
| Python | 3.12+ | `uv python install 3.12` |
| uv | 0.5+ | `curl -LsSf https://astral.sh/uv/install.sh \| sh` (Windows: `powershell -c "irm https://astral.sh/uv/install.ps1 \| iex"`) |
| Docker + Compose v2 | 24+ | Docker Desktop or Docker Engine |
| make | any | macOS/Linux built-in; Windows: use WSL2 (recommended) or Git Bash |

## First run (about 3 minutes)

```bash
git clone https://github.com/<you>/nuroflow.git && cd nuroflow
make install     # uv sync + pre-commit hooks + backend/.env from .env.example
make up          # Postgres 16 + Redis 7, waits until healthy
make migrate     # creates all 16 tables
make check       # lint + mypy strict + tests + migration drift check
```

Expected result: `267 passed`, coverage ~93%, `No new upgrade operations detected.`

## Run the API

```bash
make admin email=you@example.com   # once: creates the first admin (prompts for a password)
make api                           # http://127.0.0.1:8000/docs (auto-reload)
```

In Swagger UI click **Authorize** and paste an access token from `POST /api/v1/auth/login`,
or an API key in the `X-API-Key` field. See [AUTH.md](AUTH.md) for every flow.

## Run workflows

```bash
make worker                        # second terminal: consumes default, ai, notifications
make scheduler                     # third terminal: cron, retries, reaper, timeouts
```

Import `examples/workflows/lead-qualification.yaml` with `POST /api/v1/workflows:import` and
start it with `POST /api/v1/workflows/{id}/runs`. [ENGINE.md](ENGINE.md#running-it) has the
curl commands. LLM and Telegram calls are simulated until Phase 5.

## Daily loop

```bash
make up                      # once per session
make test-unit               # fast feedback (<1s)
make test                    # full suite incl. Postgres integration tests
make format                  # before committing (pre-commit also runs this)
```

## How the integration tests stay isolated

`tests/conftest.py` connects with `NUROFLOW_DATABASE_URL`, creates a throwaway database
`nuroflow_test_<random>`, runs real Alembic migrations into it, and drops it with `FORCE` at the
end. Your dev data is never touched, and parallel CI jobs never collide. If Postgres isn't
reachable, integration tests are **skipped** (not failed), so `make test-unit` works offline.

## Working on the schema

1. Edit models in `backend/src/nuroflow/db/models/`.
2. `make migration m="describe the change"`.
3. **Read the generated file.** Autogenerate misses some things: renames (it sees drop + add),
   data migrations, and circular FKs. See `0001` for a hand-fixed example.
4. `make migrate && make check`. `test_migrations_match_models` fails if models and migrations
   disagree.

## Inspecting the database

```bash
make db-shell                                   # psql
docker compose -f docker-compose.dev.yml --profile tools up -d adminer
# → http://localhost:8081  (server: postgres, user/pass: nuroflow)
make sql                                        # print the full migration SQL
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `port 5432 already allocated` | A local Postgres is running. Stop it, or change the host port in `docker-compose.dev.yml` and `NUROFLOW_DATABASE_URL` |
| Integration tests skipped | `make up`, then check `NUROFLOW_DATABASE_URL` in `backend/.env` and that Redis answers on `localhost:6379` (tests use DB 15) |
| `permission denied to create database` | The DB user needs `CREATEDB` (the dev compose user is a superuser) |
| Alembic runs but no tables appear | You're on an old `env.py`. See the `connection.commit()` note there (SQLAlchemy 2.0 autobegin) |
