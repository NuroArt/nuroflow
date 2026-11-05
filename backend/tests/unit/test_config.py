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
    monkeypatch.setenv("NUROFLOW_DATABASE_POOL_SIZE", "25")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.environment is Environment.PRODUCTION
    assert settings.database_pool_size == 25


def test_settings_reject_invalid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NUROFLOW_DATABASE_POOL_SIZE", "0")
    with pytest.raises(ValueError, match="database_pool_size"):
        Settings(_env_file=None)  # type: ignore[call-arg]
