"""Celery application and the single task it runs.

Run a worker::

    nuroflow-worker                       # all queues
    nuroflow-worker -Q ai -c 2            # a dedicated, low-concurrency LLM pool

Reliability settings (and why):

* ``task_acks_late`` + ``task_reject_on_worker_lost``: a message is acknowledged only
  after the task finishes, so a worker killed mid-task means redelivery, not loss.
  Duplicate deliveries are harmless thanks to the executor's compare-and-set claim.
* ``worker_prefetch_multiplier = 1``: long LLM tasks don't hoard queued messages
  while other workers sit idle.
* ``task_ignore_result``: results live in Postgres (ADR-0001), not in Redis.
* ``visibility_timeout`` above the longest task timeout, so Redis doesn't redeliver a
  message that's still legitimately running.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid

from celery.signals import worker_process_init, worker_process_shutdown

from nuroflow.core.config import get_settings
from nuroflow.engine.dispatch import EXECUTE_TASK_RUN, CeleryDispatcher
from nuroflow.notifications.delivery import DELIVER_NOTIFICATION, DeliveryClaim
from nuroflow.observability.servers import mark_child_dead
from nuroflow.workers.app_factory import create_celery_app
from nuroflow.workers.container import WorkerContainer, build_worker_container

logger = logging.getLogger(__name__)

celery_app = create_celery_app(get_settings())

_container: WorkerContainer | None = None
_container_lock = threading.Lock()


def get_container() -> WorkerContainer:
    global _container
    if _container is None:
        with _container_lock:
            if _container is None:
                _container = build_worker_container(
                    get_settings(), dispatcher=CeleryDispatcher(celery_app)
                )
    return _container


def set_container(container: WorkerContainer | None) -> None:
    """Install a pre-built container (tests) or reset it."""
    global _container
    _container = container


@worker_process_init.connect
def _reset_after_fork(**_: object) -> None:
    # Never inherit a parent's connection pool across fork().
    set_container(None)


@worker_process_shutdown.connect
def _close_container(pid: int | None = None, **_: object) -> None:
    if _container is not None:
        _container.close()
    mark_child_dead(pid or os.getpid())


@celery_app.task(name=EXECUTE_TASK_RUN)
def execute_task_run(task_run_id: str) -> None:
    container = get_container()
    follow_ups = container.executor.execute(uuid.UUID(task_run_id))
    container.dispatcher.enqueue(follow_ups)


@celery_app.task(name=DELIVER_NOTIFICATION)
def deliver_notification(delivery_id: str, attempt: int) -> None:
    get_container().deliverer.deliver(DeliveryClaim(uuid.UUID(delivery_id), attempt))
