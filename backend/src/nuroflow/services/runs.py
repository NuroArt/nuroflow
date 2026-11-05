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
from nuroflow.domain.workflow_spec import WorkflowSpec
from nuroflow.engine.dispatch import TaskDispatcher
from nuroflow.engine.planner import RunTrigger, plan_run
from nuroflow.engine.store import SPEC_CACHE
from nuroflow.operators.registry import OperatorRegistry
from nuroflow.repositories.interfaces import WorkflowUnitOfWork

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
        self._require(principal, Scope.RUNS_WRITE)
        workflow = await self._owned_workflow(principal, workflow_id)
        key = f"manual:{workflow.id}:{idempotency_key}" if idempotency_key else None
        if key and (existing := await self._uow.runs.get_by_idempotency_key(key)):
            return TriggeredRun(existing, created=False)
        if workflow.is_paused:
            raise Conflict("Workflow is paused; unpause it to start runs")
        if workflow.current_version_id is None:
            raise Conflict("Workflow has no version to run")

        spec = await self._spec(workflow.current_version_id)
        if await self._uow.runs.count_active(workflow.id) >= spec.max_active_runs:
            raise Conflict(
                f"max_active_runs ({spec.max_active_runs}) reached; wait for a run to finish"
            )

        run, requests = plan_run(
            workflow_id=workflow.id,
            version_id=workflow.current_version_id,
            spec=spec,
            trigger=RunTrigger(
                type=RunTriggerType.MANUAL,
                payload=payload,
                triggered_by_id=principal.user_id,
                idempotency_key=key,
            ),
            params=params,
            queue_for=lambda name: self._registry.get(name).default_queue,
            now=self._clock.now(),
        )
        try:
            await self._uow.runs.add(run)
            await self._uow.commit()
        except IntegrityError:
            # Lost a race with a concurrent request carrying the same Idempotency-Key.
            await self._uow.rollback()
            if key and (existing := await self._uow.runs.get_by_idempotency_key(key)):
                return TriggeredRun(existing, created=False)
            raise
        logger.info("run created", extra={"run_id": str(run.id), "workflow_id": str(workflow.id)})
        # Dispatch only after commit. The broker client does blocking I/O, so it runs
        # in a worker thread instead of on the event loop.
        await asyncio.to_thread(self._dispatcher.enqueue, requests)
        return TriggeredRun(run, created=True)

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
    async def _spec(self, version_id: uuid.UUID) -> WorkflowSpec:
        if (cached := SPEC_CACHE.get(version_id)) is not None:
            return cached
        version = await self._uow.workflows.get_version(version_id)
        if version is None:
            raise NotFound("Workflow version not found")
        spec = WorkflowSpec.model_validate(version.definition)
        SPEC_CACHE.put(version_id, spec)
        return spec

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
