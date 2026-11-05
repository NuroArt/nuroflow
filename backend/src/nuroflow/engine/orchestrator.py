"""Orchestrator: decides *what happens next* in a run. Never executes anything.

Called after every task outcome, inside the same transaction that recorded it:

1. Lock the ``workflow_runs`` row. Two upstream tasks finishing at the same instant
   serialise here, so a downstream task is evaluated exactly once.
2. Walk PENDING tasks in topological order and apply each task's trigger rule:
   READY → ``queued`` (returned for dispatch), SKIP → ``skipped``. Topological order
   means a skip cascades through the whole subtree in a single pass.
3. If every task is terminal, finalise the run (``succeeded`` / ``failed``).
"""

from __future__ import annotations

import logging
import uuid

from nuroflow.core.clock import Clock
from nuroflow.domain.enums import RunEvent, RunStatus, TaskStatus
from nuroflow.domain.state import (
    Readiness,
    ensure_run_transition,
    ensure_task_transition,
    evaluate_trigger_rule,
    final_run_status,
)
from nuroflow.engine.dispatch import EnqueueRequest
from nuroflow.engine.store import EngineStore
from nuroflow.notifications.outbox import emit

logger = logging.getLogger(__name__)


_OUTCOME_EVENTS = {
    RunStatus.SUCCEEDED: RunEvent.RUN_SUCCEEDED,
    RunStatus.FAILED: RunEvent.RUN_FAILED,
}


class Orchestrator:
    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock

    def advance(self, store: EngineStore, run_id: uuid.UUID) -> list[EnqueueRequest]:
        run = store.lock_run(run_id)
        if (
            run.status.is_terminal or run.admitted_at is None
        ):  # waiting runs only start via admission
            return []
        spec = store.spec(run.workflow_version_id)
        tasks = {t.task_key: t for t in store.task_runs(run_id)}
        now = self._clock.now()
        requests: list[EnqueueRequest] = []

        for key in spec.dag.topological_order:
            task = tasks[key]
            if task.status is not TaskStatus.PENDING:
                continue
            resolved = spec.task(key)
            upstream = [tasks[u].status for u in resolved.depends_on]
            decision = evaluate_trigger_rule(resolved.trigger_rule, upstream)
            if decision is Readiness.READY:
                ensure_task_transition(task.status, TaskStatus.QUEUED)
                task.status = TaskStatus.QUEUED
                task.queued_at = now
                requests.append(EnqueueRequest(task.id, task.queue, resolved.timeout_seconds))
            elif decision is Readiness.SKIP:
                ensure_task_transition(task.status, TaskStatus.SKIPPED)
                task.status = TaskStatus.SKIPPED
                task.finished_at = now

        outcome = final_run_status(t.status for t in tasks.values())
        if outcome is not None:
            ensure_run_transition(run.status, outcome)
            run.status = outcome
            run.finished_at = now
            run.started_at = run.started_at or now
            failed = sorted(k for k, t in tasks.items() if t.status is TaskStatus.FAILED)
            run.error_message = f"failed tasks: {', '.join(failed)}" if failed else None
            if outcome in _OUTCOME_EVENTS:
                emit(store.session, run, _OUTCOME_EVENTS[outcome], now=now)
            logger.info(
                "run finished",
                extra={"run_id": str(run.id), "status": outcome.value, "failed": failed},
            )
        elif run.status is RunStatus.PENDING and any(
            t.status is not TaskStatus.PENDING and t.status is not TaskStatus.QUEUED
            for t in tasks.values()
        ):
            ensure_run_transition(run.status, RunStatus.RUNNING)
            run.status = RunStatus.RUNNING
            run.started_at = run.started_at or now
            emit(store.session, run, RunEvent.RUN_STARTED, now=now)
        return requests
