"""``nuroflow-scheduler`` console script.

Safe to run several replicas for high availability: no leader election is needed
(see ADR-0002 and scheduler/jobs.py).
"""

from __future__ import annotations

from nuroflow.core.clock import SystemClock
from nuroflow.core.config import get_settings
from nuroflow.core.logging import configure_logging
from nuroflow.db.session import create_sync_db_engine, create_sync_session_factory
from nuroflow.engine.data_store import UnavailableDataStore
from nuroflow.engine.dispatch import CeleryDispatcher
from nuroflow.integrations.factory import build_llm_providers, build_telegram_client
from nuroflow.notifications.delivery import CelerySender
from nuroflow.operators.registry import build_registry
from nuroflow.scheduler.service import build_scheduler
from nuroflow.workers.app_factory import create_celery_app


def run() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    engine = create_sync_db_engine(settings)
    celery = create_celery_app(settings)
    # The registry is only consulted for default queues; operators never execute here.
    registry = build_registry(
        llm_providers=build_llm_providers(settings),
        telegram=build_telegram_client(settings),
        data_store=UnavailableDataStore(),
    )
    scheduler = build_scheduler(
        settings,
        session_factory=create_sync_session_factory(engine),
        dispatcher=CeleryDispatcher(celery),
        clock=SystemClock(),
        queue_for=lambda name: registry.get(name).default_queue,
        notifier=CelerySender(celery),
    )
    scheduler.install_signal_handlers()
    try:
        scheduler.run_forever()
    finally:
        engine.dispose()


if __name__ == "__main__":
    run()
