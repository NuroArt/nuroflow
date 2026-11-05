"""Run use cases: trigger (manual), inspect, cancel; task attempts, logs and stored records."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError

from nuroflow.core.clock import Clock
from nuroflow.core.exceptions import Conflict, NotFound, PermissionDenied
from nuroflow.db.models import DataRecord, TaskAttempt, TaskLog, TaskRun, Workflow, WorkflowRun
from nuroflow.domain.auth import Principal, Scope
from nuroflow.domain.enums import RunStatus, RunTriggerType
from nuroflow.engine.dispatch import TaskDispatcher
from nuroflow.engine.planner import RunTrigger
from nuroflow.operators.registry import OperatorRegistry
from nuroflow.repositories.interfaces import WorkflowUnitOfWork
from nuroflow.services.launcher import RunLauncher

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TriggeredRun:
    run: WorkflowRun
    created: bool  # False when an Idempotency-Key matched an existing run


class RunService:
    def __init__(
        self,
        uow: WorkflowUnitOfWork,
        *,
        registry: OperatorRegistry,
        dispatcher: TaskDispatcher,
        clock: Clock,
    ) -> None:
        self._uow = uow
        self._registry = registry
        self._dispatcher = dispatcher
        self._clock = clock

    async def trigger_manual(
        self,
        principal: Principal,
        workflow_id: uuid.UUID,
        *,
        params: dict[str, Any],
        payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> TriggeredRun:
        """Start a run now. Unlike automated triggers, a full workflow returns 409:
        a human is waiting for an answer, so a silent queue would be confusing."""
        self._require(principal, Scope.RUNS_WRITE)
        await self._owned_workflow(principal, workflow_id)
        key = f"manual:{workflow_id}:{idempotency_key}" if idempotency_key else None
        if key and (existing := await self._uow.runs.get_by_idempotency_key(key)):
            return TriggeredRun(existing, created=False)

        # Lock the workflow row: serialises the active-run count with other launches.
        workflow = await self._uow.workflows.get(workflow_id, lock=True)
        assert workflow is not None
        if workflow.is_paused:
            raise Conflict("Workflow is paused; unpause it to start runs")
        launcher = RunLauncher(self._uow, registry=self._registry, clock=self._clock)
        trigger = RunTrigger(
            type=RunTriggerType.MANUAL,
            payload=payload,
            triggered_by_id=principal.user_id,
            idempotency_key=key,
        )
        try:
            launch = await launcher.launch(workflow, trigger, params=params, reject_when_full=True)
            await self._uow.commit()
        except IntegrityError:
            # Lost a race with a concurrent request carrying the same Idempotency-Key.
            await self._uow.rollback()
            if key and (existing := await self._uow.runs.get_by_idempotency_key(key)):
                return TriggeredRun(existing, created=False)
            raise
        logger.info(
            "run created", extra={"run_id": str(launch.run.id), "workflow_id": str(workflow.id)}
        )
        # Dispatch only after commit. The broker client does blocking I/O, so it runs
        # in a worker thread instead of on the event loop.
        await asyncio.to_thread(self._dispatcher.enqueue, launch.requests)
        return TriggeredRun(launch.run, created=True)

    async def get(
        self, principal: Principal, run_id: uuid.UUID
    ) -> tuple[WorkflowRun, Sequence[TaskRun]]:
        self._require(principal, Scope.RUNS_READ)
        run = await self._owned_run(principal, run_id)
        return run, await self._uow.runs.task_runs(run.id)

    async def list(
        self,
        principal: Principal,
        *,
        workflow_id: uuid.UUID | None,
        status: RunStatus | None,
        limit: int,
        before_id: uuid.UUID | None,
    ) -> Sequence[WorkflowRun]:
        self._require(principal, Scope.RUNS_READ)
        return await self._uow.runs.list_page(
            owner_id=principal.user_id,
            workflow_id=workflow_id,
            status=status,
            limit=limit,
            before_id=before_id,
        )

    async def cancel(self, principal: Principal, run_id: uuid.UUID) -> WorkflowRun:
        """Cancel a run. Tasks already running finish, but nothing new starts."""
        self._require(principal, Scope.RUNS_WRITE)
        await self._owned_run(principal, run_id)
        run = await self._uow.runs.get(run_id, lock=True)  # same lock the engine takes
        assert run is not None
        if run.status.is_terminal:
            raise Conflict(f"Run is already {run.status.value}")
        now = self._clock.now()
        run.status = RunStatus.CANCELLED
        run.finished_at = now
        cancelled = await self._uow.runs.cancel_open_tasks(run.id, at=now)
        await self._uow.commit()
        logger.info("run cancelled", extra={"run_id": str(run.id), "tasks_cancelled": cancelled})
        return run

    async def attempts(self, principal: Principal, task_run_id: uuid.UUID) -> Sequence[TaskAttempt]:
        task = await self._owned_task(principal, task_run_id)
        return await self._uow.runs.attempts(task.id)

    async def logs(
        self, principal: Principal, task_run_id: uuid.UUID, *, after_id: int, limit: int
    ) -> Sequence[TaskLog]:
        task = await self._owned_task(principal, task_run_id)
        return await self._uow.runs.logs(task.id, after_id=after_id, limit=limit)

    async def records(
        self, principal: Principal, *, collection: str, limit: int, before_id: uuid.UUID | None
    ) -> Sequence[DataRecord]:
        self._require(principal, Scope.RUNS_READ)
        return await self._uow.runs.records(
            owner_id=principal.user_id, collection=collection, limit=limit, before_id=before_id
        )

    # ------------------------------------------------------------------ #
    async def _owned_workflow(
        self, principal: Principal, workflow_id: uuid.UUID, *, include_deleted: bool = False
    ) -> Workflow:
        workflow = await self._uow.workflows.get(workflow_id, include_deleted=include_deleted)
        if workflow is None or not principal.can_access_owned_by(workflow.owner_id):
            raise NotFound("Workflow not found")
        return workflow

    async def _owned_run(self, principal: Principal, run_id: uuid.UUID) -> WorkflowRun:
        run = await self._uow.runs.get(run_id)
        if run is None:
            raise NotFound("Run not found")
        try:  # history stays readable after the workflow is (soft) deleted
            await self._owned_workflow(principal, run.workflow_id, include_deleted=True)
        except NotFound:
            raise NotFound("Run not found") from None
        return run

    async def _owned_task(self, principal: Principal, task_run_id: uuid.UUID) -> TaskRun:
        self._require(principal, Scope.RUNS_READ)
        task = await self._uow.runs.get_task_run(task_run_id)
        if task is None:
            raise NotFound("Task run not found")
        try:
            await self._owned_run(principal, task.workflow_run_id)
        except NotFound:
            raise NotFound("Task run not found") from None
        return task

    @staticmethod
    def _require(principal: Principal, scope: Scope) -> None:
        if not principal.has(scope):
            raise PermissionDenied(f"Missing required scope: {scope.value}", scope=scope.value)
