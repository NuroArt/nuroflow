from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from nuroflow.api.v1.schemas.common import Schema
from nuroflow.domain.enums import AttemptStatus, LogLevel, RunStatus, RunTriggerType, TaskStatus


class RunCreate(Schema):
    params: dict[str, Any] = Field(default_factory=dict, description="Overrides spec params")
    payload: dict[str, Any] = Field(
        default_factory=dict, description="Exposed to templates as trigger.payload"
    )


class RunRead(Schema):
    id: uuid.UUID
    workflow_id: uuid.UUID
    workflow_version_id: uuid.UUID
    status: RunStatus
    trigger_type: RunTriggerType
    params: dict[str, Any]
    scheduled_for: datetime | None
    admitted_at: datetime | None = Field(
        description="null while the run waits for a max_active_runs slot"
    )
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None


class TaskRunRead(Schema):
    id: uuid.UUID
    task_key: str
    operator: str
    queue: str
    status: TaskStatus
    attempt_count: int
    max_attempts: int
    next_attempt_at: datetime | None
    queued_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    input: dict[str, Any] | None
    output: dict[str, Any] | None


class RunDetail(RunRead):
    trigger_payload: dict[str, Any]
    tasks: list[TaskRunRead]


class AttemptRead(Schema):
    id: uuid.UUID
    attempt_number: int
    status: AttemptStatus
    worker_id: str | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    error_type: str | None
    error_message: str | None
    traceback: str | None
    metrics: dict[str, Any]


class LogRead(Schema):
    id: int
    attempt_number: int
    level: LogLevel
    message: str
    context: dict[str, Any]
    created_at: datetime


class LogPage(Schema):
    items: list[LogRead]
    next_after_id: int = Field(description="Pass as ?after_id= to fetch newer lines")


class RecordRead(Schema):
    id: uuid.UUID
    collection: str
    record: dict[str, Any]
    workflow_run_id: uuid.UUID | None
    task_run_id: uuid.UUID | None
    created_at: datetime
