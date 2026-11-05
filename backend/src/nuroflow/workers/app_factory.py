"""Celery app factory, free of import-time side effects.

The API imports this to build a *producer-only* Celery app for dispatching; the
worker module (``celery_app.py``) builds the consuming app from the same function,
so producer and consumer configuration can never drift apart.
"""

from __future__ import annotations

from celery import Celery
from kombu.entity import Queue

from nuroflow.core.config import Settings

MAX_TASK_TIMEOUT_SECONDS = 3600


def create_celery_app(settings: Settings) -> Celery:
    app = Celery("nuroflow", broker=settings.broker_url)
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_ignore_result=True,
        worker_prefetch_multiplier=1,
        worker_concurrency=settings.worker_concurrency,
        worker_hijack_root_logger=False,
        task_default_queue="default",
        task_queues=[Queue(name) for name in settings.worker_queues],
        broker_connection_retry_on_startup=True,
        broker_transport_options={"visibility_timeout": MAX_TASK_TIMEOUT_SECONDS + 600},
    )
    return app
