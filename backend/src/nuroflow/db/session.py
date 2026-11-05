"""Engine and session factories (ADR-0005).

* **Async** (``postgresql+psycopg_async``) for the API process.
* **Sync** (``postgresql+psycopg``) for Celery workers and the scheduler's DB jobs (Phase 3+).

Both share the same models and the same psycopg v3 driver. Factories are plain
functions that take ``Settings``. No module-level engine exists, so importing this
module never opens a connection, and tests can build engines for throwaway databases.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from nuroflow.core.config import Settings


def _engine_kwargs(settings: Settings) -> dict[str, Any]:
    return {
        "pool_size": settings.database_pool_size,
        "max_overflow": settings.database_max_overflow,
        "pool_timeout": settings.database_pool_timeout_seconds,
        # Transparently replace connections killed by failovers or idle timeouts.
        "pool_pre_ping": True,
        "pool_recycle": 1800,
        "echo": settings.database_echo,
        "connect_args": {
            # statement_timeout: a runaway query fails instead of holding a connection
            #   (and its locks) forever.
            # timezone=UTC: timestamptz values come back in UTC whatever the server's
            #   local zone is, so API responses are consistent across environments.
            "options": (
                f"-c statement_timeout={settings.database_statement_timeout_ms} -c timezone=UTC"
            ),
            "application_name": f"nuroflow-{settings.environment.value}",
        },
    }


def create_async_db_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(settings.sqlalchemy_async_url, **_engine_kwargs(settings))


def create_async_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: objects stay readable after commit, so returning an ORM
    # object to a response schema doesn't trigger a lazy refresh (which raises in async).
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


def create_sync_db_engine(settings: Settings) -> Engine:
    return create_engine(settings.sqlalchemy_url, **_engine_kwargs(settings))


def create_sync_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False, autoflush=False)
