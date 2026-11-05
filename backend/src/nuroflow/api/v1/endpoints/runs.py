"""/runs, /task-runs, /records and /operators: execution history and the operator catalog."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from nuroflow.api.deps import Container, RunServiceDep, require_scope
from nuroflow.api.v1.schemas.common import Page, decode_cursor, encode_cursor
from nuroflow.api.v1.schemas.runs import (
    AttemptRead,
    LogPage,
    LogRead,
    RecordRead,
    RunDetail,
    RunRead,
    TaskRunRead,
)
from nuroflow.domain.auth import Principal, Scope
from nuroflow.domain.enums import RunStatus

router = APIRouter(tags=["runs"])

Reader = Annotated[Principal, Depends(require_scope(Scope.RUNS_READ))]
Writer = Annotated[Principal, Depends(require_scope(Scope.RUNS_WRITE))]


@router.get("/runs", response_model=Page[RunRead])
async def list_runs(
    principal: Reader,
    runs: RunServiceDep,
    workflow_id: uuid.UUID | None = None,
    status: RunStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> Page[RunRead]:
    rows = await runs.list(
        principal,
        workflow_id=workflow_id,
        status=status,
        limit=limit + 1,
        before_id=decode_cursor(cursor),
    )
    items = [RunRead.model_validate(r) for r in rows[:limit]]
    return Page[RunRead](
        items=items, next_cursor=encode_cursor(items[-1].id) if len(rows) > limit else None
    )


@router.get("/runs/{run_id}", response_model=RunDetail)
async def get_run(run_id: uuid.UUID, principal: Reader, runs: RunServiceDep) -> RunDetail:
    run, tasks = await runs.get(principal, run_id)
    return RunDetail(
        **RunRead.model_validate(run).model_dump(),
        trigger_payload=run.trigger_payload,
        tasks=[TaskRunRead.model_validate(t) for t in tasks],
    )


@router.post("/runs/{run_id}:cancel", response_model=RunRead)
async def cancel_run(run_id: uuid.UUID, principal: Writer, runs: RunServiceDep) -> RunRead:
    """Stop scheduling new tasks. Tasks already running are allowed to finish."""
    return RunRead.model_validate(await runs.cancel(principal, run_id))


@router.get("/task-runs/{task_run_id}/attempts", response_model=list[AttemptRead])
async def list_attempts(
    task_run_id: uuid.UUID, principal: Reader, runs: RunServiceDep
) -> list[AttemptRead]:
    """Every try of a task, with error type, message and traceback: the error tracker."""
    return [AttemptRead.model_validate(a) for a in await runs.attempts(principal, task_run_id)]


@router.get("/task-runs/{task_run_id}/logs", response_model=LogPage)
async def list_logs(
    task_run_id: uuid.UUID,
    principal: Reader,
    runs: RunServiceDep,
    after_id: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> LogPage:
    """Keyset-paginated logs. Poll with the returned next_after_id (SSE arrives in Phase 6)."""
    rows = await runs.logs(principal, task_run_id, after_id=after_id, limit=limit)
    items = [LogRead.model_validate(r) for r in rows]
    return LogPage(items=items, next_after_id=items[-1].id if items else after_id)


@router.get("/records/{collection}", response_model=Page[RecordRead])
async def list_records(
    collection: str,
    principal: Reader,
    runs: RunServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> Page[RecordRead]:
    """Records saved by `data.store` tasks, newest first."""
    rows = await runs.records(
        principal, collection=collection, limit=limit + 1, before_id=decode_cursor(cursor)
    )
    items = [RecordRead.model_validate(r) for r in rows[:limit]]
    return Page[RecordRead](
        items=items, next_cursor=encode_cursor(items[-1].id) if len(rows) > limit else None
    )


@router.get("/operators", tags=["operators"])
async def list_operators(_: Reader, container: Container) -> list[dict[str, Any]]:
    """Installed task types with their JSON input schemas (powers editor autocomplete)."""
    return container.registry.describe()
