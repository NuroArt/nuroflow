# ADR-0005: Async API, sync workers, shared models

- **Status:** Accepted
- **Date:** 2026-11-04

## Context

FastAPI shines with `async` handlers, especially for SSE log streaming and many concurrent clients.
Celery's execution model is synchronous: async inside Celery tasks needs an event loop per task
and is a common source of subtle bugs.

## Decision

- **API:** SQLAlchemy `AsyncSession` over `psycopg` (async mode).
- **Workers, scheduler-internal sync helpers, Alembic:** SQLAlchemy `Session` over `psycopg` (sync).
- **One set of ORM models** (`nuroflow.db.models`) serves both.
- The domain layer (`WorkflowSpec`, DAG, state machine, retry math) is pure and I/O-free, so it
  is shared without any sync/async split.
- Repositories split along the same line: `repositories/sqlalchemy.py` (async) for API
  read/write paths, and sync repositories for engine paths (Phase 3). The engine and the
  API need different queries anyway (the engine locks rows; the API paginates history),
  so duplication is minimal.

## Alternatives

| Option | Trade-off |
|---|---|
| Everything sync (FastAPI threadpool) | Simpler, and perfectly viable at this scale. Rejected only because SSE tailing and high-fanout reads benefit from async |
| Everything async (arq / Celery with asyncio) | Unifies the code, but arq has a smaller ecosystem and the brief requires Celery |

## Consequences

- ✅ Each process uses the idiomatic model for its runtime.
- ⚠️ Two session factories to configure. That's centralized in `db/session.py` (Phase 2).
