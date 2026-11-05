"""Shared pytest fixtures.

Integration tests run against a real PostgreSQL, never SQLite, because the schema
depends on Postgres features (JSONB, partial indexes, CHECKs on arrays, transactional
DDL). Each test session creates a throwaway database, ``<name>_test_<random>``, and
drops it afterwards, so tests never touch your dev data.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import Session

from nuroflow.core.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[1]


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


@pytest.fixture
def migrated_db(alembic_config: Config) -> Iterator[None]:
    """Schema at ``head`` for the duration of one test, then back to an empty schema."""
    from alembic import command

    command.upgrade(alembic_config, "head")
    yield
    command.downgrade(alembic_config, "base")


@pytest.fixture
def session(engine: Engine, migrated_db: None) -> Iterator[Session]:
    with Session(engine) as s:
        yield s
        s.rollback()
