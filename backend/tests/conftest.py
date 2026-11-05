"""Shared pytest fixtures.

Integration tests run against a real PostgreSQL, never SQLite, because the schema
depends on Postgres features (JSONB, partial indexes, CHECKs on arrays, transactional
DDL). Each test session creates a throwaway database, ``<name>_test_<random>``, and
drops it afterwards, so tests never touch your dev data.

Fixture layers:

* ``database_url`` / ``alembic_config`` / ``engine``: session-scoped plumbing.
* ``migrated_db`` / ``session``: schema tests (Phase 1). Upgrade, then downgrade per test.
* ``container`` / ``client``: API tests (Phase 2). Real app, real DB and Redis,
  in-memory rate limiter. Tables are truncated after each test.

If Postgres or Redis isn't reachable, the dependent tests are *skipped*, so
``pytest tests/unit`` always works offline.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from redis import Redis as SyncRedis
from redis.exceptions import RedisError
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import Session

from nuroflow.api.container import AppContainer, build_container
from nuroflow.api.main import create_app
from nuroflow.core.config import Environment, Settings, get_settings
from nuroflow.core.rate_limit import InMemoryRateLimiter
from nuroflow.db.models import Base

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_REDIS_URL = os.environ.get("NUROFLOW_TEST_REDIS_URL", "redis://localhost:6379/15")


# --------------------------------------------------------------------------- #
# Database plumbing
# --------------------------------------------------------------------------- #
def _admin_url() -> URL:
    return make_url(get_settings().sqlalchemy_url)


def _postgres_available(url: URL) -> bool:
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool, connect_args={"connect_timeout": 2})
    try:
        with engine.connect():
            return True
    except sa.exc.OperationalError:
        return False
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> Iterator[URL]:
    """Create an isolated database for this test session and drop it at the end."""
    admin_url = _admin_url()
    if not _postgres_available(admin_url):
        pytest.skip(f"PostgreSQL not reachable at {admin_url.render_as_string(hide_password=True)}")

    test_db = f"{admin_url.database}_test_{secrets.token_hex(4)}"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool)
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{test_db}"'))
    try:
        yield admin_url.set(database=test_db)
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{test_db}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def alembic_config(database_url: URL) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option(
        "sqlalchemy.url", database_url.render_as_string(hide_password=False).replace("%", "%%")
    )
    return cfg


@pytest.fixture(scope="session")
def engine(database_url: URL) -> Iterator[Engine]:
    eng = sa.create_engine(database_url, poolclass=sa.pool.NullPool)
    yield eng
    eng.dispose()


# --------------------------------------------------------------------------- #
# Schema tests (Phase 1)
# --------------------------------------------------------------------------- #
@pytest.fixture
def migrated_db(alembic_config: Config) -> Iterator[None]:
    """Schema at ``head`` for the duration of one test, then back to an empty schema."""
    command.upgrade(alembic_config, "head")
    yield
    command.downgrade(alembic_config, "base")


@pytest.fixture
def session(engine: Engine, migrated_db: None) -> Iterator[Session]:
    with Session(engine) as s:
        yield s
        s.rollback()


# --------------------------------------------------------------------------- #
# API tests (Phase 2)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def redis_url() -> str:
    client = SyncRedis.from_url(TEST_REDIS_URL, socket_connect_timeout=1)
    try:
        client.ping()
    except RedisError:
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}")
    finally:
        client.close()
    return TEST_REDIS_URL


@pytest.fixture(scope="session")
def api_settings(database_url: URL, redis_url: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment=Environment.TEST,
        database_url=database_url.render_as_string(hide_password=False),  # type: ignore[arg-type]
        redis_url=redis_url,  # type: ignore[arg-type]
        database_pool_size=5,
        login_max_attempts_per_identity=5,
    )


@pytest.fixture
def rate_limiter() -> InMemoryRateLimiter:
    return InMemoryRateLimiter()


@pytest.fixture
async def container(
    api_settings: Settings,
    alembic_config: Config,
    engine: Engine,
    rate_limiter: InMemoryRateLimiter,
) -> AsyncIterator[AppContainer]:
    command.upgrade(alembic_config, "head")  # no-op when already at head
    built = build_container(api_settings, rate_limiter=rate_limiter)
    try:
        yield built
    finally:
        await built.aclose()
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        with engine.begin() as conn:
            conn.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def client(container: AppContainer) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(container=container)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
