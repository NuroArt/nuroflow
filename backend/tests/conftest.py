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
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from redis import Redis as SyncRedis
from redis.exceptions import RedisError
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from nuroflow.api.container import AppContainer, build_container
from nuroflow.api.main import create_app
from nuroflow.core.clock import FrozenClock
from nuroflow.core.config import Environment, Settings, get_settings
from nuroflow.core.ids import uuid7
from nuroflow.core.rate_limit import InMemoryRateLimiter
from nuroflow.db.models import Base, TaskRun, User, Workflow, WorkflowRun, WorkflowVersion
from nuroflow.domain.enums import RunTriggerType
from nuroflow.domain.workflow_spec import WorkflowSpec
from nuroflow.engine.data_store import SqlDataStore
from nuroflow.engine.executor import TaskExecutor
from nuroflow.engine.inline import InlineDispatcher
from nuroflow.engine.orchestrator import Orchestrator
from nuroflow.engine.planner import RunTrigger, plan_run
from nuroflow.integrations.llm.fake import FakeLLMProvider
from nuroflow.integrations.telegram.fake import FakeTelegramClient
from nuroflow.operators.registry import OperatorRegistry, build_registry

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
    harness: EngineHarness,
) -> AsyncIterator[AppContainer]:
    command.upgrade(alembic_config, "head")  # no-op when already at head
    # Runs triggered over HTTP execute synchronously on the real engine (inline dispatch),
    # sharing the harness's fake LLM/Telegram so tests can script and inspect them.
    built = build_container(
        api_settings,
        rate_limiter=rate_limiter,
        dispatcher=harness.dispatcher,
        registry=harness.registry,
    )
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


# --------------------------------------------------------------------------- #
# Engine tests (Phase 3)
# --------------------------------------------------------------------------- #
@dataclass
class EngineHarness:
    """A real engine (executor + orchestrator + SQL) with fake providers and inline dispatch."""

    session_factory: sessionmaker[Session]
    executor: TaskExecutor
    dispatcher: InlineDispatcher
    clock: FrozenClock
    llm: FakeLLMProvider
    telegram: FakeTelegramClient
    registry: OperatorRegistry
    owner_id: uuid.UUID = field(default_factory=uuid7)

    def create_workflow(self, definition: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID]:
        spec = WorkflowSpec.model_validate(definition)
        with self.session_factory() as s, s.begin():
            if s.get(User, self.owner_id) is None:
                s.add(
                    User(id=self.owner_id, email=f"{self.owner_id.hex}@t.io", hashed_password="x")
                )
                s.flush()
            wf = Workflow(id=uuid7(), owner_id=self.owner_id, slug=spec.slug, name=spec.name)
            s.add(wf)
            s.flush()
            version = WorkflowVersion(
                id=uuid7(),
                workflow_id=wf.id,
                version=1,
                definition=spec.model_dump(mode="json"),
                checksum=spec.checksum(),
            )
            s.add(version)
            s.flush()
            wf.current_version_id = version.id
            return wf.id, version.id

    def start_run(
        self,
        definition: dict[str, Any],
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        dispatch: bool = True,
    ) -> uuid.UUID:
        workflow_id, version_id = self.create_workflow(definition)
        run, requests = plan_run(
            workflow_id=workflow_id,
            version_id=version_id,
            spec=WorkflowSpec.model_validate(definition),
            trigger=RunTrigger(type=RunTriggerType.MANUAL, payload=payload or {}),
            params=params or {},
            queue_for=lambda name: self.registry.get(name).default_queue,
            now=self.clock.now(),
        )
        with self.session_factory() as s, s.begin():
            s.add(run)
        if dispatch:
            self.dispatcher.enqueue(requests)
        return run.id

    def run(self, run_id: uuid.UUID) -> tuple[WorkflowRun, dict[str, TaskRun]]:
        with self.session_factory() as s:
            run = s.get(WorkflowRun, run_id)
            assert run is not None
            tasks = s.scalars(sa.select(TaskRun).where(TaskRun.workflow_run_id == run_id)).all()
            return run, {t.task_key: t for t in tasks}


@pytest.fixture
def sync_sessions(engine: Engine, alembic_config: Config) -> Iterator[sessionmaker[Session]]:
    command.upgrade(alembic_config, "head")
    yield sessionmaker(engine, expire_on_commit=False)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def harness(sync_sessions: sessionmaker[Session]) -> EngineHarness:
    clock = FrozenClock(datetime.now(UTC))
    llm, telegram = FakeLLMProvider(), FakeTelegramClient()
    registry = build_registry(
        llm_providers={"fake": llm, "openai": llm, "anthropic": llm},
        telegram=telegram,
        data_store=SqlDataStore(sync_sessions),
        load_plugins=False,
    )
    executor = TaskExecutor(
        session_factory=sync_sessions,
        registry=registry,
        orchestrator=Orchestrator(clock=clock),
        clock=clock,
        heartbeat_interval_seconds=0,
    )
    return EngineHarness(
        session_factory=sync_sessions,
        executor=executor,
        dispatcher=InlineDispatcher(executor),
        clock=clock,
        llm=llm,
        telegram=telegram,
        registry=registry,
    )
