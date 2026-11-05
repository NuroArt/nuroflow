"""Workflow use cases: validate, create, version, list, pause, delete."""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from nuroflow.core.clock import Clock
from nuroflow.core.crypto import SecretBox
from nuroflow.core.exceptions import Conflict, NotFound, PermissionDenied, ValidationError
from nuroflow.core.ids import uuid7
from nuroflow.db.models import Workflow, WorkflowTrigger, WorkflowVersion
from nuroflow.domain.auth import AuthMethod, Principal, Scope
from nuroflow.domain.enums import TriggerType
from nuroflow.domain.schedule import next_fire_after
from nuroflow.domain.workflow_spec import (
    CronTriggerSpec,
    EventTriggerSpec,
    WebhookTriggerSpec,
    WorkflowSpec,
)
from nuroflow.operators.registry import OperatorRegistry
from nuroflow.repositories.interfaces import WorkflowUnitOfWork

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SpecIssue:
    loc: str
    message: str


@dataclass(frozen=True, slots=True)
class ValidationReport:
    spec: WorkflowSpec | None
    issues: list[SpecIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.spec is not None and not self.issues


def validate_definition(definition: dict[str, Any], registry: OperatorRegistry) -> ValidationReport:
    """Two passes: the spec's own rules (pure), then installed-operator checks."""
    try:
        spec = WorkflowSpec.model_validate(definition)
    except PydanticValidationError as exc:
        return ValidationReport(
            spec=None,
            issues=[
                SpecIssue(".".join(str(p) for p in err["loc"]) or "<root>", err["msg"])
                for err in exc.errors()
            ],
        )

    issues: list[SpecIssue] = []
    for index, task in enumerate(spec.tasks):
        if task.operator not in registry:
            issues.append(
                SpecIssue(
                    f"tasks.{index}.operator",
                    f"unknown operator '{task.operator}' (installed: {', '.join(registry.names)})",
                )
            )
            continue
        operator_cls = type(registry.get(task.operator))
        issues += [
            SpecIssue(f"tasks.{index}.inputs", f"{task.key}: {problem}")
            for problem in operator_cls.static_check(task.inputs)
        ]
    return ValidationReport(spec=spec, issues=issues)


class WorkflowService:
    def __init__(
        self,
        uow: WorkflowUnitOfWork,
        *,
        registry: OperatorRegistry,
        clock: Clock,
        secrets: SecretBox,
    ) -> None:
        self._uow = uow
        self._registry = registry
        self._clock = clock
        self._secrets = secrets

    def validate(self, definition: dict[str, Any]) -> ValidationReport:
        return validate_definition(definition, self._registry)

    async def create(
        self, principal: Principal, *, definition: dict[str, Any], change_note: str | None = None
    ) -> tuple[Workflow, WorkflowVersion]:
        self._require(principal, Scope.WORKFLOWS_WRITE)
        spec = self._valid_spec(definition)
        if await self._uow.workflows.get_live_by_slug(principal.user_id, spec.slug) is not None:
            raise Conflict(f"A workflow with slug '{spec.slug}' already exists", field="slug")

        workflow = Workflow(id=uuid7(), owner_id=principal.user_id, slug=spec.slug, is_paused=False)
        _apply_metadata(workflow, spec)
        await self._uow.workflows.add(workflow)
        version = await self._add_version(workflow, spec, 1, principal, change_note)
        await self._sync_triggers(workflow, spec)
        await self._uow.commit()
        logger.info("workflow created", extra={"workflow_id": str(workflow.id), "slug": spec.slug})
        return workflow, version

    async def add_version(
        self,
        principal: Principal,
        workflow_id: uuid.UUID,
        *,
        definition: dict[str, Any],
        change_note: str | None = None,
    ) -> tuple[WorkflowVersion, bool]:
        """Returns ``(version, created)``. An identical definition is a no-op."""
        self._require(principal, Scope.WORKFLOWS_WRITE)
        # Row lock: two concurrent saves can't both compute version N+1.
        workflow = await self._owned(principal, workflow_id, lock=True)
        spec = self._valid_spec(definition)
        if spec.slug != workflow.slug:
            raise ValidationError(
                f"slug cannot change (expected '{workflow.slug}', got '{spec.slug}')", field="slug"
            )
        if workflow.current_version_id is not None:
            current = await self._uow.workflows.get_version(workflow.current_version_id)
            if current is not None and current.checksum == spec.checksum():
                return current, False

        number = await self._uow.workflows.max_version(workflow.id) + 1
        version = await self._add_version(workflow, spec, number, principal, change_note)
        _apply_metadata(workflow, spec)
        await self._sync_triggers(workflow, spec)
        await self._uow.commit()
        logger.info(
            "workflow version created",
            extra={"workflow_id": str(workflow.id), "version": number},
        )
        return version, True

    async def get(
        self, principal: Principal, workflow_id: uuid.UUID
    ) -> tuple[Workflow, WorkflowVersion | None]:
        self._require(principal, Scope.WORKFLOWS_READ)
        workflow = await self._owned(principal, workflow_id)
        version = (
            await self._uow.workflows.get_version(workflow.current_version_id)
            if workflow.current_version_id
            else None
        )
        return workflow, version

    async def list(
        self, principal: Principal, *, limit: int, before_id: uuid.UUID | None
    ) -> Sequence[Workflow]:
        self._require(principal, Scope.WORKFLOWS_READ)
        return await self._uow.workflows.list_page(
            owner_id=principal.user_id, limit=limit, before_id=before_id
        )

    async def versions(
        self, principal: Principal, workflow_id: uuid.UUID
    ) -> Sequence[WorkflowVersion]:
        self._require(principal, Scope.WORKFLOWS_READ)
        await self._owned(principal, workflow_id)
        return await self._uow.workflows.list_versions(workflow_id)

    async def set_paused(
        self, principal: Principal, workflow_id: uuid.UUID, *, paused: bool
    ) -> Workflow:
        self._require(principal, Scope.WORKFLOWS_WRITE)
        workflow = await self._owned(principal, workflow_id)
        workflow.is_paused = paused
        await self._uow.commit()
        return workflow

    async def delete(self, principal: Principal, workflow_id: uuid.UUID) -> None:
        """Soft delete: run history stays queryable, and the slug becomes reusable."""
        self._require(principal, Scope.WORKFLOWS_WRITE)
        workflow = await self._owned(principal, workflow_id)
        workflow.deleted_at = self._clock.now()
        workflow.is_paused = True
        await self._uow.commit()

    # ---- triggers ------------------------------------------------------ #
    async def triggers(
        self, principal: Principal, workflow_id: uuid.UUID
    ) -> Sequence[WorkflowTrigger]:
        self._require(principal, Scope.WORKFLOWS_READ)
        await self._owned(principal, workflow_id)
        return await self._uow.workflows.list_triggers(workflow_id)

    async def webhook_secret(
        self, principal: Principal, trigger_id: uuid.UUID, *, rotate: bool = False
    ) -> tuple[WorkflowTrigger, str]:
        """Reveal (or rotate) a webhook signing secret. Interactive login only."""
        self._require(principal, Scope.WORKFLOWS_WRITE)
        if principal.method is not AuthMethod.JWT:
            raise PermissionDenied("Webhook secrets can only be read from an interactive login")
        trigger = await self._uow.workflows.get_trigger(trigger_id)
        if trigger is None or trigger.type is not TriggerType.WEBHOOK:
            raise NotFound("Webhook trigger not found")
        await self._owned(principal, trigger.workflow_id)
        if rotate or trigger.webhook_secret_encrypted is None:
            trigger.webhook_secret_encrypted = self._secrets.encrypt(_new_webhook_secret())
            await self._uow.commit()
        assert trigger.webhook_secret_encrypted is not None
        return trigger, self._secrets.decrypt(trigger.webhook_secret_encrypted)

    async def _sync_triggers(self, workflow: Workflow, spec: WorkflowSpec) -> None:
        """Make workflow_triggers match the spec, keyed by trigger identity (spec_key).

        * Removed from the spec → deleted. Run history keeps its runs (trigger_id SET NULL).
        * Added → created (cron gets its first next_fire_at; webhook gets a new secret).
        * Kept → updated in place: a webhook keeps its URL and secret, and a cron keeps
          its position in time, across versions.
        """
        existing = {t.spec_key: t for t in await self._uow.workflows.list_triggers(workflow.id)}
        desired = {t.spec_key: t for t in spec.triggers}
        for key in existing.keys() - desired.keys():
            await self._uow.workflows.delete_trigger(existing[key])
        now = self._clock.now()
        for key, wanted in desired.items():
            row = existing.get(key)
            is_new = row is None
            if row is None:
                row = WorkflowTrigger(
                    id=uuid7(), workflow_id=workflow.id, type=TriggerType(wanted.type), spec_key=key
                )
            # Populate every field *before* the INSERT: the table's CHECK constraints
            # (e.g. event ⇒ event_type) are evaluated at flush time.
            row.is_enabled = True
            if isinstance(wanted, CronTriggerSpec):
                row.cron_expression = wanted.cron
                row.timezone = wanted.timezone
                row.catchup = wanted.catchup
                if is_new:
                    row.next_fire_at = next_fire_after(wanted.cron, wanted.timezone, now)
            elif isinstance(wanted, EventTriggerSpec):
                row.event_type, row.event_filter = wanted.event_type, wanted.event_filter
            elif isinstance(wanted, WebhookTriggerSpec):
                row.name = wanted.name
                if is_new:
                    row.webhook_secret_encrypted = self._secrets.encrypt(_new_webhook_secret())
            if is_new:
                await self._uow.workflows.add_trigger(row)

    # ------------------------------------------------------------------ #
    def _valid_spec(self, definition: dict[str, Any]) -> WorkflowSpec:
        report = self.validate(definition)
        if not report.valid or report.spec is None:
            raise ValidationError(
                "Workflow definition is invalid",
                errors=[{"loc": i.loc, "msg": i.message} for i in report.issues],
            )
        return report.spec

    async def _add_version(
        self,
        workflow: Workflow,
        spec: WorkflowSpec,
        number: int,
        principal: Principal,
        change_note: str | None,
    ) -> WorkflowVersion:
        version = WorkflowVersion(
            id=uuid7(),
            workflow_id=workflow.id,
            version=number,
            definition=spec.model_dump(mode="json"),
            checksum=spec.checksum(),
            change_note=change_note,
            created_by_id=principal.user_id,
        )
        await self._uow.workflows.add_version(version)
        workflow.current_version_id = version.id
        return version

    async def _owned(
        self, principal: Principal, workflow_id: uuid.UUID, *, lock: bool = False
    ) -> Workflow:
        workflow = await self._uow.workflows.get(workflow_id, lock=lock)
        if workflow is None or not principal.can_access_owned_by(workflow.owner_id):
            raise NotFound("Workflow not found")
        return workflow

    @staticmethod
    def _require(principal: Principal, scope: Scope) -> None:
        if not principal.has(scope):
            raise PermissionDenied(f"Missing required scope: {scope.value}", scope=scope.value)


def _new_webhook_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def _apply_metadata(workflow: Workflow, spec: WorkflowSpec) -> None:
    workflow.name = spec.name
    workflow.description = spec.description
    workflow.tags = list(spec.tags)
    workflow.max_active_runs = spec.max_active_runs
