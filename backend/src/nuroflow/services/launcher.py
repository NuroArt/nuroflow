"""Shared run-creation path for every async trigger: manual, event and webhook.

One place decides idempotency, admission and planning, so triggers can't drift apart
in behaviour. The caller owns the transaction: it must hold the workflow row lock
(serialising the active-run count) and commits before dispatching the returned requests.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from nuroflow.core.clock import Clock
from nuroflow.core.exceptions import Conflict, NotFound
from nuroflow.db.models import Workflow, WorkflowRun
from nuroflow.domain.workflow_spec import WorkflowSpec
from nuroflow.engine.dispatch import EnqueueRequest
from nuroflow.engine.planner import RunTrigger, plan_run
from nuroflow.engine.store import SPEC_CACHE
from nuroflow.operators.registry import OperatorRegistry
from nuroflow.repositories.interfaces import WorkflowUnitOfWork


@dataclass(frozen=True, slots=True)
class Launch:
    run: WorkflowRun
    requests: list[EnqueueRequest]
    created: bool  # False: idempotency key matched an existing run
    admitted: bool  # False: waiting for a max_active_runs slot


class RunLauncher:
    def __init__(
        self, uow: WorkflowUnitOfWork, *, registry: OperatorRegistry, clock: Clock
    ) -> None:
        self._uow = uow
        self._registry = registry
        self._clock = clock

    async def launch(
        self,
        workflow: Workflow,
        trigger: RunTrigger,
        *,
        params: dict[str, Any] | None = None,
        reject_when_full: bool,
    ) -> Launch:
        """Create a run. ``reject_when_full`` raises 409 instead of queueing (manual runs)."""
        if trigger.idempotency_key and (
            existing := await self._uow.runs.get_by_idempotency_key(trigger.idempotency_key)
        ):
            return Launch(existing, [], created=False, admitted=existing.admitted_at is not None)
        if workflow.current_version_id is None:
            raise Conflict("Workflow has no version to run")

        spec = await self.spec(workflow.current_version_id)
        active = await self._uow.runs.count_admitted_active(workflow.id)
        admit = active < spec.max_active_runs
        if not admit and reject_when_full:
            raise Conflict(
                f"max_active_runs ({spec.max_active_runs}) reached; wait for a run to finish"
            )
        run, requests = plan_run(
            workflow_id=workflow.id,
            version_id=workflow.current_version_id,
            spec=spec,
            trigger=trigger,
            params=params or {},
            queue_for=lambda name: self._registry.get(name).default_queue,
            now=self._clock.now(),
            admit=admit,
        )
        await self._uow.runs.add(run)
        return Launch(run, requests, created=True, admitted=admit)

    async def spec(self, version_id: uuid.UUID) -> WorkflowSpec:
        if (cached := SPEC_CACHE.get(version_id)) is not None:
            return cached
        version = await self._uow.workflows.get_version(version_id)
        if version is None:
            raise NotFound("Workflow version not found")
        spec = WorkflowSpec.model_validate(version.definition)
        SPEC_CACHE.put(version_id, spec)
        return spec
