"""Application settings.

Phase 1 defines only what the database layer and migrations need. Phase 2 extends this
module with API, security, Redis, and integration settings. The *pattern* is fixed now:

* Every setting is read from the environment with the ``NUROFLOW_`` prefix (12-factor).
* A local ``.env`` file is honoured for development only.
* Settings are validated once at startup; a bad value fails fast with a clear error.
* ``get_settings()`` is cached and injected, never imported as a global, so tests can
  override it.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NUROFLOW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = Environment.LOCAL

    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+psycopg://nuroflow:nuroflow@localhost:5432/nuroflow"),
        description="SQLAlchemy URL. Must use the psycopg (v3) driver.",
    )
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    database_echo: bool = False

    @property
    def sqlalchemy_url(self) -> str:
        """Database URL as a plain string, normalised to the psycopg v3 driver."""
        url = str(self.database_url)
        for legacy in ("postgresql://", "postgres://", "postgresql+psycopg2://"):
            if url.startswith(legacy):
                return "postgresql+psycopg://" + url.removeprefix(legacy)
        return url


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached; override in tests)."""
    return Settings()
