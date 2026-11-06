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
    for key, value in DEPLOYED.items():
        monkeypatch.setenv(f"NUROFLOW_{key.upper()}", value)
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


# A complete, safe production configuration. Each test below breaks exactly one thing.
DEPLOYED: dict[str, str] = {
    "secret_key": "s" * 48,
    "metrics_token": "m" * 32,
    "database_url": "postgresql+psycopg://nuroflow:Str0ng-db-pass@db:5432/nuroflow",
    "redis_url": "redis://:Str0ng-redis-pass@redis:6379/0",
    "encryption_keys": "q2Xfy9VuQbWm1E7bq6l1Hc8b1n2bJ1Jz6kQ0aG9cRKs=",
}


def deployed(**overrides: object) -> Settings:
    return Settings(_env_file=None, environment="production", **{**DEPLOYED, **overrides})  # type: ignore[arg-type, call-arg]


def test_deployed_environment_accepts_a_safe_configuration() -> None:
    assert deployed().environment.is_deployed


@pytest.mark.parametrize(
    ("override", "message"),
    [
        (
            {"database_url": "postgresql+psycopg://nuroflow:nuroflow@db:5432/nuroflow"},
            "DATABASE_URL",
        ),
        ({"redis_url": "redis://redis:6379/0"}, "REDIS_URL"),
        ({"celery_broker_url": "redis://redis:6379/1"}, "REDIS_URL"),
        ({"encryption_keys": ""}, "ENCRYPTION_KEYS"),
        ({"cors_origins": "*"}, "CORS_ORIGINS"),
        ({"llm_mode": "simulated"}, "must be 'live'"),
    ],
)
def test_deployed_environment_rejects_each_unsafe_default(
    override: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        deployed(**override)


def test_deployed_metrics_need_a_token_unless_disabled() -> None:
    with pytest.raises(ValueError, match="NUROFLOW_METRICS_TOKEN"):
        deployed(metrics_token=None)
    assert not deployed(metrics_token=None, metrics_enabled=False).metrics_enabled


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
