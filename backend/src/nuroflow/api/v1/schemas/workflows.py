from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from nuroflow.api.v1.schemas.common import Schema


class WorkflowDefinitionIn(Schema):
    definition: dict[str, Any] = Field(
        description="A WorkflowSpec document (see examples/workflows)"
    )
    change_note: str | None = Field(default=None, max_length=500)


class WorkflowRead(Schema):
    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    tags: list[str]
    is_paused: bool
    max_active_runs: int
    current_version_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class VersionRead(Schema):
    id: uuid.UUID
    version: int
    checksum: str
    change_note: str | None
    created_by_id: uuid.UUID | None
    created_at: datetime


class VersionDetail(VersionRead):
    definition: dict[str, Any]


class WorkflowDetail(WorkflowRead):
    current_version: VersionDetail | None


class VersionCreated(Schema):
    version: VersionRead
    created: bool = Field(description="False when the definition was identical to the current one")


class SpecIssueRead(Schema):
    loc: str
    message: str


class ValidationResult(Schema):
    valid: bool
    issues: list[SpecIssueRead]
    task_order: list[str] | None = Field(
        default=None, description="Topological execution order, when valid"
    )


class WorkflowPause(Schema):
    paused: bool
