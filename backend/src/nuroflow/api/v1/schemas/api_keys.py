from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, computed_field

from nuroflow.api.v1.schemas.common import Schema
from nuroflow.domain.auth import Scope


class ApiKeyCreate(Schema):
    name: str = Field(min_length=1, max_length=100, examples=["zapier-lead-form"])
    scopes: list[Scope] = Field(min_length=1, examples=[["events:write"]])
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class ApiKeyRead(Schema):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hint(self) -> str:
        """Safe-to-display identifier, e.g. ``nf_ab12cd34ef56_…``."""
        return f"nf_{self.prefix}_…"


class ApiKeyCreated(ApiKeyRead):
    key: str = Field(description="The full API key. Shown only once: store it securely now.")
