"""/workflows: definitions, immutable versions, validation, YAML import, manual runs."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from nuroflow.api.deps import RunServiceDep, WorkflowServiceDep, require_scope
from nuroflow.api.v1.schemas.common import Page, decode_cursor, encode_cursor
from nuroflow.api.v1.schemas.runs import RunCreate, RunRead
from nuroflow.api.v1.schemas.workflows import (
    SpecIssueRead,
    ValidationResult,
    VersionCreated,
    VersionDetail,
    VersionRead,
    WorkflowDefinitionIn,
    WorkflowDetail,
    WorkflowPause,
    WorkflowRead,
)
from nuroflow.core.exceptions import ValidationError
from nuroflow.db.models import Workflow, WorkflowVersion
from nuroflow.domain.auth import Principal, Scope

router = APIRouter(prefix="/workflows", tags=["workflows"])

Reader = Annotated[Principal, Depends(require_scope(Scope.WORKFLOWS_READ))]
Writer = Annotated[Principal, Depends(require_scope(Scope.WORKFLOWS_WRITE))]
RunWriter = Annotated[Principal, Depends(require_scope(Scope.RUNS_WRITE))]

MAX_YAML_BYTES = 256 * 1024


def _detail(workflow: Workflow, version: WorkflowVersion | None) -> WorkflowDetail:
    base = WorkflowRead.model_validate(workflow).model_dump()
    return WorkflowDetail(
        **base, current_version=VersionDetail.model_validate(version) if version else None
    )


@router.post(":validate", response_model=ValidationResult)
async def validate_workflow(
    body: WorkflowDefinitionIn, _: Reader, workflows: WorkflowServiceDep
) -> ValidationResult:
    """Dry-run validation. Always 200; check `valid` and `issues`."""
    report = workflows.validate(body.definition)
    return ValidationResult(
        valid=report.valid,
        issues=[SpecIssueRead(loc=i.loc, message=i.message) for i in report.issues],
        task_order=list(report.spec.dag.topological_order)
        if report.valid and report.spec
        else None,
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=WorkflowDetail)
async def create_workflow(
    body: WorkflowDefinitionIn, principal: Writer, workflows: WorkflowServiceDep
) -> WorkflowDetail:
    workflow, version = await workflows.create(
        principal, definition=body.definition, change_note=body.change_note
    )
    return _detail(workflow, version)


@router.post(
    ":import",
    status_code=status.HTTP_201_CREATED,
    response_model=WorkflowDetail,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/yaml": {"schema": {"type": "string"}}},
        }
    },
)
async def import_yaml(
    request: Request, principal: Writer, workflows: WorkflowServiceDep
) -> WorkflowDetail:
    """Create a workflow from a raw YAML file::

    curl -X POST …/workflows:import -H 'Content-Type: application/yaml' \\
         --data-binary @examples/workflows/lead-qualification.yaml
    """
    definition = await _read_yaml(request)
    workflow, version = await workflows.create(principal, definition=definition)
    return _detail(workflow, version)


@router.get("", response_model=Page[WorkflowRead])
async def list_workflows(
    principal: Reader,
    workflows: WorkflowServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> Page[WorkflowRead]:
    rows = await workflows.list(principal, limit=limit + 1, before_id=decode_cursor(cursor))
    items = [WorkflowRead.model_validate(w) for w in rows[:limit]]
    return Page[WorkflowRead](
        items=items, next_cursor=encode_cursor(items[-1].id) if len(rows) > limit else None
    )


@router.get("/{workflow_id}", response_model=WorkflowDetail)
async def get_workflow(
    workflow_id: uuid.UUID, principal: Reader, workflows: WorkflowServiceDep
) -> WorkflowDetail:
    return _detail(*await workflows.get(principal, workflow_id))


@router.post("/{workflow_id}/versions", response_model=VersionCreated)
async def create_version(
    workflow_id: uuid.UUID,
    body: WorkflowDefinitionIn,
    principal: Writer,
    workflows: WorkflowServiceDep,
    response: Response,
) -> VersionCreated:
    """Publish a new immutable version. Running runs keep using the version they started on."""
    version, created = await workflows.add_version(
        principal, workflow_id, definition=body.definition, change_note=body.change_note
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return VersionCreated(version=VersionRead.model_validate(version), created=created)


@router.get("/{workflow_id}/versions", response_model=list[VersionRead])
async def list_versions(
    workflow_id: uuid.UUID, principal: Reader, workflows: WorkflowServiceDep
) -> list[VersionRead]:
    return [VersionRead.model_validate(v) for v in await workflows.versions(principal, workflow_id)]


@router.post("/{workflow_id}:pause", response_model=WorkflowRead)
async def pause_workflow(
    workflow_id: uuid.UUID, body: WorkflowPause, principal: Writer, workflows: WorkflowServiceDep
) -> WorkflowRead:
    workflow = await workflows.set_paused(principal, workflow_id, paused=body.paused)
    return WorkflowRead.model_validate(workflow)


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(
    workflow_id: uuid.UUID, principal: Writer, workflows: WorkflowServiceDep
) -> None:
    """Soft delete. Run history stays available under /runs."""
    await workflows.delete(principal, workflow_id)


@router.post("/{workflow_id}/runs", status_code=status.HTTP_202_ACCEPTED, response_model=RunRead)
async def trigger_run(
    workflow_id: uuid.UUID,
    body: RunCreate,
    principal: RunWriter,
    runs: RunServiceDep,
    response: Response,
    idempotency_key: Annotated[
        str | None, Header(max_length=200, description="Retry-safe: same key → same run")
    ] = None,
) -> RunRead:
    """Start a run now. Returns 202 (accepted for execution), or 200 when the
    Idempotency-Key matched an existing run."""
    result = await runs.trigger_manual(
        principal,
        workflow_id,
        params=body.params,
        payload=body.payload,
        idempotency_key=idempotency_key,
    )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return RunRead.model_validate(result.run)


async def _read_yaml(request: Request) -> dict[str, Any]:
    body = await request.body()
    if len(body) > MAX_YAML_BYTES:
        raise ValidationError(f"YAML document exceeds {MAX_YAML_BYTES // 1024} KB")
    try:
        # safe_load: plain data only, so YAML tags can't instantiate Python objects.
        document = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        raise ValidationError(f"Invalid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise ValidationError("YAML document must be a mapping")
    return document
