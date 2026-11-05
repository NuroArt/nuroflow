"""Run planning: turn a workflow version into a run with one TaskRun per task.

Pure in the sense that matters: it builds ORM objects but performs no I/O, so the
async API and the sync scheduler (Phase 4) share it unchanged. IDs are pre-generated
(UUIDv7), so the enqueue requests are known before the transaction commits.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from nuroflow.core.ids import uuid7
from nuroflow.db.models import TaskRun, WorkflowRun
from nuroflow.domain.enums import RunStatus, RunTriggerType, TaskStatus
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
) -> tuple[WorkflowRun, list[EnqueueRequest]]:
    """Build the run and its task runs. Root tasks start ``queued``, the rest ``pending``."""
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
        deadline_at=now + timedelta(seconds=spec.timeout_seconds) if spec.timeout_seconds else None,
    )
    roots = set(spec.dag.roots)
    requests: list[EnqueueRequest] = []
    task_runs: list[TaskRun] = []
    for task in spec.resolved_tasks():
        queue = task.queue or queue_for(task.operator)
        is_root = task.key in roots
        task_run = TaskRun(
            id=uuid7(),
            workflow_run_id=run.id,
            task_key=task.key,
            operator=task.operator,
            queue=queue,
            status=TaskStatus.QUEUED if is_root else TaskStatus.PENDING,
            attempt_count=0,
            max_attempts=task.max_attempts,
            queued_at=now if is_root else None,
        )
        task_runs.append(task_run)
        if is_root:
            requests.append(EnqueueRequest(task_run.id, queue, task.timeout_seconds))
    run.task_runs = task_runs
    return run, requests
