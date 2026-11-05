"""App factory, lifespan, failure modes and the management CLI."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import httpx
import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import Engine

from nuroflow.api.container import AppContainer, build_container
from nuroflow.api.main import app_factory, create_app
from nuroflow.cli import main as cli_main
from nuroflow.core.config import Settings, get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def env_settings(
    api_settings: Settings, alembic_config: Config, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Point environment-driven code paths (app_factory, CLI) at the test database."""
    command.upgrade(alembic_config, "head")
    monkeypatch.setenv("NUROFLOW_DATABASE_URL", api_settings.sqlalchemy_url)
    monkeypatch.setenv("NUROFLOW_REDIS_URL", str(api_settings.redis_url))
    monkeypatch.setenv("NUROFLOW_ENVIRONMENT", "test")
    get_settings.cache_clear()
    root_handlers = logging.getLogger().handlers[:]
    yield
    logging.getLogger().handlers[:] = root_handlers
    get_settings.cache_clear()


async def test_app_factory_lifespan_builds_and_closes_container(env_settings: None) -> None:
    app = app_factory()
    # Drive the real ASGI lifespan exactly as uvicorn would.
    async with app.router.lifespan_context(app):
        container = app.state.container
        assert isinstance(container, AppContainer)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            assert (await client.get("/readyz")).status_code == 200
    # Shutdown disposed the pool: nothing is left checked out.
    assert container.engine.pool.checkedout() == 0


async def test_readyz_reports_503_when_redis_is_down(api_settings: Settings) -> None:
    broken = build_container(api_settings.model_copy(update={"redis_url": "redis://127.0.0.1:1/0"}))
    app = create_app(container=broken)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as client:
            response = await client.get("/readyz")
    finally:
        await broken.aclose()
    assert response.status_code == 503
    checks = response.json()["checks"]
    assert checks["database"]["status"] == "ok"
    assert checks["redis"]["status"] == "error"
    assert "127.0.0.1" not in response.text  # never leak connection details


async def test_unhandled_exception_returns_generic_500(container: AppContainer) -> None:
    app = create_app(container=container)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("database password is hunter2")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://t"
    ) as client:
        response = await client.get("/boom")
    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert "hunter2" not in response.text


async def test_docs_can_be_disabled(container: AppContainer) -> None:
    settings = container.settings.model_copy(update={"docs_enabled": False})
    app = create_app(settings, container=container)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404


def test_cli_create_admin_then_promote(
    env_settings: None,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NUROFLOW_ADMIN_PASSWORD", "cli-admin-password-1")
    try:
        assert cli_main(["create-admin", "--email", "Root@NuroWorks.com"]) == 0
        assert "Created admin root@nuroworks.com" in capsys.readouterr().out

        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE users SET role = 'viewer', is_active = false"))
        assert cli_main(["create-admin", "--email", "root@nuroworks.com"]) == 0
        assert "Promoted existing user" in capsys.readouterr().out

        with engine.connect() as conn:
            row = conn.execute(sa.text("SELECT role, is_active FROM users")).one()
        assert tuple(row) == ("admin", True)

        monkeypatch.setenv("NUROFLOW_ADMIN_PASSWORD", "short")
        assert cli_main(["create-admin", "--email", "other@nuroworks.com"]) == 1
        assert "at least 12 characters" in capsys.readouterr().err
    finally:
        with engine.begin() as conn:
            conn.execute(sa.text("TRUNCATE users CASCADE"))
