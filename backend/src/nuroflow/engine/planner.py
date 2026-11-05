"""Run planning and admission: turn a workflow version into a run, then let it start.

Pure in the sense that matters: it builds and mutates ORM objects but performs no I/O,
so the async API and the sync scheduler share it unchanged. IDs are pre-generated
(UUIDv7), so the enqueue requests are known before the transaction commits.

**Admission (backpressure).** ``max_active_runs`` caps *admitted* runs. A run created
by automation (cron, event, webhook) when the workflow is full is still persisted, but
*waiting*: ``admitted_at`` is NULL and no task is queued. The scheduler admits waiting
runs oldest-first as slots free up. A burst of 500 leads is queued and processed,
never dropped. Manual runs fail fast with 409 instead, because a human is waiting.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from nuroflow.core.ids import uuid7
from nuroflow.db.models import TaskRun, WorkflowRun
from nuroflow.domain.enums import RunStatus, RunTriggerType, TaskStatus
from nuroflow.domain.state import ensure_task_transition
from nuroflow.domain.workflow_spec import WorkflowSpec
from nuroflow.engine.dispatch import EnqueueRequest


@dataclass(frozen=True, slots=True)
class RunTrigger:
    type: RunTriggerType
    payload: dict[str, Any]
    trigger_id: uuid.UUID | None = None
    event_id: uuid.UUID | None = None
    triggered_by_id: uuid.UUID | None = None
    idempotency_key: str | None = None
    scheduled_for: datetime | None = None


def plan_run(
    *,
    workflow_id: uuid.UUID,
    version_id: uuid.UUID,
    spec: WorkflowSpec,
    trigger: RunTrigger,
    params: dict[str, Any],
    queue_for: Callable[[str], str],
    now: datetime,
    admit: bool = True,
) -> tuple[WorkflowRun, list[EnqueueRequest]]:
    """Build the run with every task ``pending``; if ``admit``, admit it immediately."""
    run = WorkflowRun(
        id=uuid7(),
        workflow_id=workflow_id,
        workflow_version_id=version_id,
        trigger_type=trigger.type,
        trigger_id=trigger.trigger_id,
        event_id=trigger.event_id,
        triggered_by_id=trigger.triggered_by_id,
        status=RunStatus.PENDING,
        params={**spec.params, **params},
        trigger_payload=trigger.payload,
        idempotency_key=trigger.idempotency_key,
        scheduled_for=trigger.scheduled_for,
    )
    run.task_runs = [
        TaskRun(
            id=uuid7(),
            workflow_run_id=run.id,
            task_key=task.key,
            operator=task.operator,
            queue=task.queue or queue_for(task.operator),
            status=TaskStatus.PENDING,
            attempt_count=0,
            max_attempts=task.max_attempts,
        )
        for task in spec.resolved_tasks()
    ]
    requests = admit_run(run, run.task_runs, spec, now=now) if admit else []
    return run, requests


def admit_run(
    run: WorkflowRun, task_runs: Sequence[TaskRun], spec: WorkflowSpec, *, now: datetime
) -> list[EnqueueRequest]:
    """Admit a waiting run: start its clock and queue its root tasks."""
    if run.admitted_at is not None:
        raise ValueError(f"run {run.id} is already admitted")
    run.admitted_at = now
    if spec.timeout_seconds:  # the deadline counts from admission, not from waiting
        run.deadline_at = now + timedelta(seconds=spec.timeout_seconds)
    roots = set(spec.dag.roots)
    requests: list[EnqueueRequest] = []
    for task_run in task_runs:
        if task_run.task_key in roots:
            ensure_task_transition(task_run.status, TaskStatus.QUEUED)
            task_run.status = TaskStatus.QUEUED
            task_run.queued_at = now
            timeout = spec.task(task_run.task_key).timeout_seconds
            requests.append(EnqueueRequest(task_run.id, task_run.queue, timeout))
    return requests
