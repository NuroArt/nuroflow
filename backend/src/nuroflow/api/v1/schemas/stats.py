from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from nuroflow.api.v1.schemas.common import Schema


class SeriesPoint(Schema):
    at: datetime
    succeeded: int
    failed: int
    other: int = Field(description="pending, running, cancelled")


class FailingWorkflow(Schema):
    workflow_id: uuid.UUID
    name: str
    failed: int
    total: int


class Overview(Schema):
    since: datetime
    bucket: Literal["hour", "day"]
    runs: int
    by_status: dict[str, int]
    success_rate: float | None = Field(description="succeeded / (succeeded + failed)")
    duration_p50_seconds: float | None
    duration_p95_seconds: float | None
    active_runs: int
    queued_tasks: int
    series: list[SeriesPoint]
    failing_workflows: list[FailingWorkflow]
