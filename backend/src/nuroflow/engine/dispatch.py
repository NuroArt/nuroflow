"""Task dispatch: how a ``task_run_id`` reaches a worker.

The engine decides *what* is ready; a dispatcher decides *how* it's delivered:

* :class:`CeleryDispatcher`: production. ``send_task`` by name, so the API process
  never imports worker code.
* :class:`RecordingDispatcher`: tests that assert on what was enqueued.
* ``engine.inline.InlineDispatcher``: runs tasks synchronously in-process (tests, demos).

Dispatch always happens **after** the transaction that queued the task has committed.
If dispatch fails (broker down), the task stays ``queued`` in Postgres and the reaper
(Phase 4) re-enqueues it. A lost message costs latency, never correctness (ADR-0001).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from celery import Celery

logger = logging.getLogger(__name__)

EXECUTE_TASK_RUN = "nuroflow.execute_task_run"
HARD_LIMIT_GRACE_SECONDS = 30


@dataclass(frozen=True, slots=True)
class EnqueueRequest:
    task_run_id: uuid.UUID
    queue: str
    timeout_seconds: int


class TaskDispatcher(Protocol):
    def enqueue(self, requests: Sequence[EnqueueRequest]) -> None: ...


class CeleryDispatcher:
    def __init__(self, app: Celery) -> None:
        self._app = app

    def enqueue(self, requests: Sequence[EnqueueRequest]) -> None:
        for req in requests:
            try:
                self._app.send_task(
                    EXECUTE_TASK_RUN,
                    args=[str(req.task_run_id)],
                    queue=req.queue,
                    # soft limit raises inside the task (we record a timeout and retry);
                    # the hard limit kills a task that ignores the soft one.
                    soft_time_limit=req.timeout_seconds,
                    time_limit=req.timeout_seconds + HARD_LIMIT_GRACE_SECONDS,
                )
            except Exception:
                logger.exception(
                    "dispatch failed; the reaper will re-enqueue",
                    extra={"task_run_id": str(req.task_run_id), "queue": req.queue},
                )


class RecordingDispatcher:
    def __init__(self) -> None:
        self.requests: list[EnqueueRequest] = []

    def enqueue(self, requests: Sequence[EnqueueRequest]) -> None:
        self.requests.extend(requests)
