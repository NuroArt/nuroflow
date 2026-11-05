"""Application settings.

* Every setting comes from the environment with the ``NUROFLOW_`` prefix (12-factor).
  A local ``.env`` file is honoured for development only.
* Settings are validated once at startup, so a bad value fails fast with a clear error
  instead of surfacing as a 500 at 3 a.m.
* ``get_settings()`` is cached and *injected* (FastAPI ``Depends`` / the app factory),
  never imported as a mutable global, so tests can build their own ``Settings``.
* Unsafe development defaults (secret key, open registration, API docs) are allowed
  in ``local``/``test`` but **rejected** in ``staging``/``production``.
"""

from __future__ import annotations

import hashlib
import hmac
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Self

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

INSECURE_DEV_SECRET = "insecure-dev-secret-change-me-0000000000000000000000000000"  # noqa: S105


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_deployed(self) -> bool:
        return self in {Environment.STAGING, Environment.PRODUCTION}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NUROFLOW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- runtime ----------------------------------------------------------- #
    environment: Environment = Environment.LOCAL
    app_name: str = "NuroFlow"
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR)$")
    log_json: bool = Field(default=False, description="JSON logs (enable when deployed)")

    # ---- database (Phase 1) ------------------------------------------------- #
    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+psycopg://nuroflow:nuroflow@localhost:5432/nuroflow"),
        description="SQLAlchemy URL. Normalised to the psycopg (v3) driver.",
    )
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    database_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    database_statement_timeout_ms: int = Field(default=30_000, ge=0)
    database_echo: bool = False

    # ---- redis (Phase 2) ---------------------------------------------------- #
    redis_url: RedisDsn = Field(default=RedisDsn("redis://localhost:6379/0"))

    # ---- HTTP API (Phase 2) ------------------------------------------------- #
    api_v1_prefix: str = "/api/v1"
    docs_enabled: bool = True
    # NoDecode: read as "a,b,c" instead of JSON, which is friendlier in .env files.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    # ---- security (Phase 2) ------------------------------------------------- #
    secret_key: SecretStr = Field(
        default=SecretStr(INSECURE_DEV_SECRET),
        description="Root secret. Purpose-specific keys are derived from it (see derive_key).",
    )
    jwt_algorithm: str = Field(default="HS256", pattern=r"^HS(256|384|512)$")
    jwt_issuer: str = "nuroflow"
    jwt_audience: str = "nuroflow-api"
    access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    refresh_token_ttl_seconds: int = Field(default=14 * 24 * 3600, ge=3600)
    registration_enabled: bool = True
    password_min_length: int = Field(default=12, ge=8)

    # ---- abuse protection (Phase 2) --------------------------------------- #
    login_max_attempts_per_identity: int = Field(default=10, ge=1)
    login_max_attempts_per_ip: int = Field(default=50, ge=1)
    login_window_seconds: int = Field(default=300, ge=1)

    # ---- workflow engine (Phase 3) -------------------------------------------- #
    celery_broker_url: str | None = Field(
        default=None, description="Defaults to redis_url. Set to use a separate broker DB."
    )
    worker_concurrency: int = Field(default=4, ge=1, le=256)
    worker_queues: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["default", "ai", "notifications"]
    )
    task_output_max_bytes: int = Field(default=256 * 1024, ge=1024, le=10 * 1024 * 1024)
    task_heartbeat_interval_seconds: float = Field(default=15.0, ge=0)
    http_allow_private_networks: bool = Field(
        default=False, description="Allow http.request to reach private IPs (local dev only)."
    )

    # ---- scheduler, events & webhooks (Phase 4) -------------------------------- #
    public_base_url: str = Field(
        default="http://localhost:8000", description="Used to build webhook URLs shown to users"
    )
    scheduler_tick_seconds: float = Field(default=2.0, gt=0, le=60)
    scheduler_batch_size: int = Field(default=100, ge=1, le=10_000)
    cron_max_catchup_runs: int = Field(default=50, ge=1, le=10_000)
    reaper_queued_timeout_seconds: int = Field(default=600, ge=10)
    reaper_heartbeat_timeout_seconds: int = Field(default=90, ge=5)
    webhook_tolerance_seconds: int = Field(default=300, ge=30, le=3600)
    event_rate_limit_per_minute: int = Field(default=600, ge=1)
    webhook_rate_limit_per_minute: int = Field(default=120, ge=1)
    # Fernet keys, newest first. Empty → one key derived from secret_key. Listing an old
    # key after a new one lets you rotate without breaking existing ciphertexts.
    encryption_keys: Annotated[list[SecretStr], NoDecode] = Field(default_factory=list)

    # --------------------------------------------------------------------- #
    @model_validator(mode="before")
    @classmethod
    def _split_comma_lists(cls, data: object) -> object:
        if isinstance(data, dict):
            for name in ("cors_origins", "worker_queues", "encryption_keys"):
                raw = data.get(name)
                if isinstance(raw, str):
                    data[name] = [o.strip() for o in raw.split(",") if o.strip()]
        return data

    @model_validator(mode="after")
    def _heartbeat_outlives_reaper_threshold(self) -> Self:
        hb = self.task_heartbeat_interval_seconds
        if hb > 0 and self.reaper_heartbeat_timeout_seconds < 3 * hb:
            raise ValueError(
                "reaper_heartbeat_timeout_seconds must be at least 3x "
                "task_heartbeat_interval_seconds, or healthy tasks get reaped"
            )
        return self

    @model_validator(mode="after")
    def _reject_unsafe_defaults_when_deployed(self) -> Self:
        if not self.environment.is_deployed:
            return self
        problems: list[str] = []
        secret = self.secret_key.get_secret_value()
        if secret == INSECURE_DEV_SECRET or len(secret) < 32:
            problems.append("NUROFLOW_SECRET_KEY must be set to a random value of 32+ characters")
        if "*" in self.cors_origins:
            problems.append("NUROFLOW_CORS_ORIGINS must not contain '*' when deployed")
        if self.http_allow_private_networks:
            problems.append(
                "NUROFLOW_HTTP_ALLOW_PRIVATE_NETWORKS must be false when deployed (SSRF)"
            )
        if problems:
            raise ValueError(f"unsafe configuration for {self.environment}: " + "; ".join(problems))
        return self

    # --------------------------------------------------------------------- #
    @property
    def sqlalchemy_url(self) -> str:
        """Database URL (sync driver) as a plain string, normalised to psycopg v3."""
        url = str(self.database_url)
        for legacy in ("postgresql://", "postgres://", "postgresql+psycopg2://"):
            if url.startswith(legacy):
                return "postgresql+psycopg://" + url.removeprefix(legacy)
        return url

    @property
    def sqlalchemy_async_url(self) -> str:
        """psycopg v3 serves sync and async from the same dialect name (ADR-0005)."""
        return self.sqlalchemy_url.replace("postgresql+psycopg://", "postgresql+psycopg_async://")

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or str(self.redis_url)

    def derive_key(self, purpose: str) -> bytes:
        """Derive an independent 256-bit key for one purpose from ``secret_key``.

        Key separation: a leaked JWT signing key must not also let an attacker forge
        API-key hashes. HMAC-SHA256(root, purpose) is a standard KDF for
        high-entropy roots.
        """
        root = self.secret_key.get_secret_value().encode()
        return hmac.new(root, f"nuroflow:{purpose}".encode(), hashlib.sha256).digest()


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached; override in tests)."""
    return Settings()
