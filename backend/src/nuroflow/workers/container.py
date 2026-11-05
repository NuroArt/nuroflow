"""Composition root for worker processes (the sync twin of ``api/container.py``).

Built lazily, once per worker *process*. With Celery's prefork pool, that means
after fork, so no database connection or socket is ever shared between parent and child.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from nuroflow.core.clock import Clock, SystemClock
from nuroflow.core.config import Settings
from nuroflow.core.crypto import SecretBox
from nuroflow.db.session import create_sync_db_engine, create_sync_session_factory
from nuroflow.engine.data_store import SqlDataStore
from nuroflow.engine.dispatch import TaskDispatcher
from nuroflow.engine.executor import TaskExecutor
from nuroflow.engine.orchestrator import Orchestrator
from nuroflow.integrations.credentials import SqlCredentialResolver
from nuroflow.integrations.factory import build_llm_providers, build_telegram_client
from nuroflow.integrations.llm.base import LLMProvider
from nuroflow.integrations.telegram.base import TelegramClient
from nuroflow.notifications.delivery import NotificationDeliverer, build_transport
from nuroflow.operators.registry import OperatorRegistry, build_registry


@dataclass(slots=True)
class WorkerContainer:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    registry: OperatorRegistry
    executor: TaskExecutor
    dispatcher: TaskDispatcher
    clock: Clock
    deliverer: NotificationDeliverer

    def close(self) -> None:
        self.engine.dispose()


def build_worker_container(
    settings: Settings,
    *,
    dispatcher: TaskDispatcher,
    clock: Clock | None = None,
    llm_providers: Mapping[str, LLMProvider] | None = None,
    telegram: TelegramClient | None = None,
    registry: OperatorRegistry | None = None,
) -> WorkerContainer:
    clock = clock or SystemClock()
    engine = create_sync_db_engine(settings)
    sessions = create_sync_session_factory(engine)
    secrets = SecretBox.from_settings(settings)
    telegram = telegram or build_telegram_client(settings)
    registry = registry or build_registry(
        llm_providers=llm_providers if llm_providers is not None else build_llm_providers(settings),
        telegram=telegram,
        data_store=SqlDataStore(sessions),
        allow_private_networks=settings.http_allow_private_networks,
        credentials=SqlCredentialResolver.from_settings(sessions, settings, secrets),
        llm_pricing=settings.llm_pricing,
    )
    executor = TaskExecutor(
        session_factory=sessions,
        registry=registry,
        orchestrator=Orchestrator(clock=clock),
        clock=clock,
        output_max_bytes=settings.task_output_max_bytes,
        heartbeat_interval_seconds=settings.task_heartbeat_interval_seconds,
    )
    return WorkerContainer(
        settings=settings,
        engine=engine,
        session_factory=sessions,
        registry=registry,
        executor=executor,
        dispatcher=dispatcher,
        clock=clock,
        deliverer=build_deliverer(
            settings, sessions, telegram=telegram, secrets=secrets, clock=clock
        ),
    )


def build_deliverer(
    settings: Settings,
    sessions: sessionmaker[Session],
    *,
    telegram: TelegramClient,
    secrets: SecretBox,
    clock: Clock,
) -> NotificationDeliverer:
    return NotificationDeliverer(
        session_factory=sessions,
        transport=build_transport(settings, telegram=telegram, clock=clock),
        secrets=secrets,
        clock=clock,
        max_attempts=settings.notification_max_attempts,
    )
