"""Live views: ``/runs/{id}/stream`` (run + task status) and ``/task-runs/{id}/logs/stream``.

Both authorize **once** with the request's session, then release it. Each poll then
opens its own short-lived session, so a stream never pins a pooled connection for
minutes. Hundreds of open dashboards cost hundreds of tiny queries per second, not
hundreds of held connections.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from nuroflow.api.deps import Container, RunServiceDep, UoW, require_scope
from nuroflow.api.sse import SSEEvent, event_stream, sse_response
from nuroflow.api.v1.schemas.runs import LogRead, RunRead
from nuroflow.domain.auth import Principal, Scope
from nuroflow.repositories.sqlalchemy import SqlAlchemyUnitOfWork

router = APIRouter(tags=["runs"])
Reader = Annotated[Principal, Depends(require_scope(Scope.RUNS_READ))]

LOG_BATCH = 500
# Inputs and outputs can be up to 256 KB per task: the stream sends status, not payloads.
_TASK_FIELDS = (
    "id", "task_key", "operator", "queue", "status", "attempt_count", "max_attempts",
    "next_attempt_at", "queued_at", "started_at", "finished_at", "error_message",
)  # fmt: skip


@router.get("/runs/{run_id}/stream", response_class=StreamingResponse)
async def stream_run(
    run_id: uuid.UUID,
    request: Request,
    principal: Reader,
    runs: RunServiceDep,
    uow: UoW,
    container: Container,
) -> StreamingResponse:
    """``event: snapshot`` whenever the run or any task changes; ``event: end`` when done."""
    await runs.get(principal, run_id)  # authorization (404 if not yours)
    await uow.rollback()  # release the request's connection before streaming
    last_fingerprint: str | None = None

    async def source() -> tuple[list[SSEEvent], bool]:
        nonlocal last_fingerprint
        async with container.session_factory() as session:
            repo = SqlAlchemyUnitOfWork(session).runs
            run = await repo.get(run_id)
            tasks = await repo.task_runs(run_id) if run else []
        if run is None:  # deleted mid-stream
            return [], True
        snapshot: dict[str, Any] = {
            **RunRead.model_validate(run).model_dump(mode="json"),
            "tasks": [
                {f: _jsonable(getattr(t, f)) for f in _TASK_FIELDS}
                for t in sorted(tasks, key=lambda t: t.task_key)
            ],
        }
        fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
        events = []
        if fingerprint != last_fingerprint:
            last_fingerprint = fingerprint
            events.append(SSEEvent("snapshot", snapshot))
        return events, run.status.is_terminal

    return sse_response(
        event_stream(
            source,
            is_disconnected=request.is_disconnected,
            poll_seconds=container.settings.sse_poll_seconds,
            max_seconds=container.settings.sse_max_stream_seconds,
        )
    )


@router.get("/task-runs/{task_run_id}/logs/stream", response_class=StreamingResponse)
async def stream_logs(
    task_run_id: uuid.UUID,
    request: Request,
    principal: Reader,
    runs: RunServiceDep,
    uow: UoW,
    container: Container,
    after_id: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """``event: log`` per line (``id`` = log id, so reconnects resume exactly), ``event: status``
    when the task changes state, ``event: end`` once the task is final and drained."""
    await runs.logs(principal, task_run_id, after_id=0, limit=1)  # authorization
    await uow.rollback()
    cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else after_id
    last_status: tuple[str, int] | None = None

    async def source() -> tuple[list[SSEEvent], bool]:
        nonlocal cursor, last_status
        async with container.session_factory() as session:
            repo = SqlAlchemyUnitOfWork(session).runs
            task = await repo.get_task_run(task_run_id)
            rows = await repo.logs(task_run_id, after_id=cursor, limit=LOG_BATCH)
        if task is None:
            return [], True
        events = [
            SSEEvent("log", LogRead.model_validate(r).model_dump(mode="json"), id=str(r.id))
            for r in rows
        ]
        if rows:
            cursor = rows[-1].id
        status = (task.status.value, task.attempt_count)
        if status != last_status:
            last_status = status
            events.append(SSEEvent("status", {"status": status[0], "attempt_count": status[1]}))
        # Finished only when the task is final *and* every log line has been sent.
        return events, task.status.is_terminal and len(rows) < LOG_BATCH

    return sse_response(
        event_stream(
            source,
            is_disconnected=request.is_disconnected,
            poll_seconds=container.settings.sse_poll_seconds,
            max_seconds=container.settings.sse_max_stream_seconds,
        )
    )


def _jsonable(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return getattr(value, "value", value)
