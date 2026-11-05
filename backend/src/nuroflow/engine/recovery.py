"""Recovery jobs run by the scheduler loop (Phase 4).

They're defined here, next to the engine rules they rely on, so they're unit-testable
without the scheduler process.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from nuroflow.core.clock import Clock
from nuroflow.domain.enums import TaskStatus
from nuroflow.domain.state import ensure_task_transition
from nuroflow.engine.dispatch import EnqueueRequest
from nuroflow.engine.store import EngineStore


def dispatch_due_retries(
    session_factory: sessionmaker[Session], *, clock: Clock, limit: int = 200
) -> list[EnqueueRequest]:
    """Move up_for_retry tasks whose backoff has elapsed back to queued (ADR-0003).

    Safe to run on several schedulers at once, because rows are claimed with SKIP LOCKED.
    The caller dispatches the returned requests *after* this function's commit.
    """
    now = clock.now()
    requests: list[EnqueueRequest] = []
    with session_factory() as session, session.begin():
        store = EngineStore(session)
        for task in store.due_retries(now, limit):
            ensure_task_transition(task.status, TaskStatus.QUEUED)
            task.status = TaskStatus.QUEUED
            task.queued_at = now
            task.next_attempt_at = None
            spec = store.spec(store.version_id_for_run(task.workflow_run_id))
            requests.append(
                EnqueueRequest(task.id, task.queue, spec.task(task.task_key).timeout_seconds)
            )
    return requests
