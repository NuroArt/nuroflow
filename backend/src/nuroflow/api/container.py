"""Composition root for the API process.

Every long-lived object is built **once** here: engine, Redis client, hashers, JWT
service, rate limiter. FastAPI dependencies then read from ``app.state.container``.
This is dependency injection without a framework. Tests construct a container with
swapped parts (in-memory rate limiter, frozen clock) and pass it to ``create_app``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from nuroflow.core.clock import Clock, SystemClock
from nuroflow.core.config import Settings
from nuroflow.core.crypto import SecretBox
from nuroflow.core.rate_limit import RateLimiter, RedisRateLimiter
from nuroflow.core.security import ApiKeyCodec, JWTService, PasswordHasher
from nuroflow.db.session import create_async_db_engine, create_async_session_factory
from nuroflow.engine.data_store import UnavailableDataStore
from nuroflow.engine.dispatch import CeleryDispatcher, TaskDispatcher
from nuroflow.integrations.factory import build_llm_providers, build_telegram_client
from nuroflow.operators.registry import OperatorRegistry, build_registry
from nuroflow.services.auth import AuthPolicy
from nuroflow.workers.app_factory import create_celery_app


@dataclass(slots=True)
class AppContainer:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    rate_limiter: RateLimiter
    clock: Clock
    password_hasher: PasswordHasher
    jwt: JWTService
    api_key_codec: ApiKeyCodec
    auth_policy: AuthPolicy
    registry: OperatorRegistry
    dispatcher: TaskDispatcher
    secrets: SecretBox

    async def aclose(self) -> None:
        await self.redis.aclose()
        await self.engine.dispose()


def build_container(
    settings: Settings,
    *,
    clock: Clock | None = None,
    rate_limiter: RateLimiter | None = None,
    dispatcher: TaskDispatcher | None = None,
    registry: OperatorRegistry | None = None,
) -> AppContainer:
    engine = create_async_db_engine(settings)
    redis = Redis.from_url(
        str(settings.redis_url),
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
        decode_responses=True,
    )
    return AppContainer(
        settings=settings,
        engine=engine,
        session_factory=create_async_session_factory(engine),
        redis=redis,
        rate_limiter=rate_limiter or RedisRateLimiter(redis),
        clock=clock or SystemClock(),
        password_hasher=PasswordHasher(),
        jwt=JWTService(
            signing_key=settings.derive_key("jwt"),
            algorithm=settings.jwt_algorithm,
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            ttl=timedelta(seconds=settings.access_token_ttl_seconds),
        ),
        api_key_codec=ApiKeyCodec(pepper=settings.derive_key("api-keys")),
        auth_policy=AuthPolicy(
            refresh_token_ttl=timedelta(seconds=settings.refresh_token_ttl_seconds),
            registration_enabled=settings.registration_enabled,
            password_min_length=settings.password_min_length,
        ),
        # The API validates and describes operators but never executes them, so
        # data.store gets a placeholder that refuses to run.
        registry=registry
        or build_registry(
            llm_providers=build_llm_providers(settings),
            telegram=build_telegram_client(settings),
            data_store=UnavailableDataStore(),
            allow_private_networks=settings.http_allow_private_networks,
        ),
        # Producer-only Celery app: the API publishes task_run_ids; workers consume them.
        dispatcher=dispatcher or CeleryDispatcher(create_celery_app(settings)),
        secrets=SecretBox.from_settings(settings),
    )
