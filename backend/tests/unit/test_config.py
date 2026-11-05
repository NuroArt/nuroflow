from __future__ import annotations

import pytest

from nuroflow.core.config import Environment, Settings


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgresql://u:p@db:5432/nf", "postgresql+psycopg://u:p@db:5432/nf"),
        ("postgres://u:p@db:5432/nf", "postgresql+psycopg://u:p@db:5432/nf"),
        ("postgresql+psycopg2://u:p@db:5432/nf", "postgresql+psycopg://u:p@db:5432/nf"),
        ("postgresql+psycopg://u:p@db:5432/nf", "postgresql+psycopg://u:p@db:5432/nf"),
    ],
)
def test_sqlalchemy_url_is_normalised_to_psycopg3(raw: str, expected: str) -> None:
    settings = Settings(database_url=raw)  # type: ignore[arg-type]
    assert settings.sqlalchemy_url == expected


def test_settings_read_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NUROFLOW_ENVIRONMENT", "production")
    monkeypatch.setenv("NUROFLOW_SECRET_KEY", "a-real-secret-" + "x" * 40)
    monkeypatch.setenv("NUROFLOW_DATABASE_POOL_SIZE", "25")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.environment is Environment.PRODUCTION
    assert settings.database_pool_size == 25


def test_settings_reject_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NUROFLOW_DATABASE_POOL_SIZE", "0")
    with pytest.raises(ValueError, match="database_pool_size"):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployed_environments_reject_the_dev_secret(environment: str) -> None:
    with pytest.raises(ValueError, match="NUROFLOW_SECRET_KEY"):
        Settings(_env_file=None, environment=environment)  # type: ignore[call-arg, arg-type]


def test_deployed_environment_accepts_a_real_secret() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        environment="production",
        secret_key="s" * 48,  # type: ignore[arg-type]
    )
    assert settings.environment.is_deployed


def test_cors_origins_accept_comma_separated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NUROFLOW_CORS_ORIGINS", "https://app.nuroworks.com, http://localhost:5173")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.cors_origins == ["https://app.nuroworks.com", "http://localhost:5173"]


def test_derived_keys_are_independent_per_purpose() -> None:
    settings = Settings(_env_file=None, secret_key="r" * 48)  # type: ignore[call-arg, arg-type]
    jwt_key, api_key = settings.derive_key("jwt"), settings.derive_key("api-keys")
    assert jwt_key != api_key
    assert len(jwt_key) == 32
    assert settings.derive_key("jwt") == jwt_key  # deterministic across restarts


def test_async_url_uses_psycopg_async_dialect() -> None:
    settings = Settings(database_url="postgresql://u:p@db/nf")  # type: ignore[arg-type]
    assert settings.sqlalchemy_async_url == "postgresql+psycopg_async://u:p@db/nf"
