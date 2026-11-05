from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from nuroflow.api.v1.schemas.common import Schema
from nuroflow.api.v1.schemas.runs import RunRead
from nuroflow.domain.enums import TriggerType


class TriggerRead(Schema):
    id: uuid.UUID
    type: TriggerType
    name: str | None
    is_enabled: bool
    cron_expression: str | None
    timezone: str
    catchup: bool
    next_fire_at: datetime | None
    last_fired_at: datetime | None
    event_type: str | None
    event_filter: dict[str, Any] | None
    webhook_url: str | None = None


class WebhookSecret(Schema):
    trigger_id: uuid.UUID
    webhook_url: str
    secret: str = Field(description="HMAC-SHA256 signing secret. Treat like a password.")
    signature_header: str = "X-NuroFlow-Signature: sha256=hex(hmac(secret, f'{timestamp}.{body}'))"
    timestamp_header: str = "X-NuroFlow-Timestamp: <unix seconds>"


class EventIn(Schema):
    type: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,199}$", examples=["lead.captured"])
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str | None = Field(default=None, max_length=200, examples=["website-form"])


class EventAccepted(Schema):
    event_id: uuid.UUID
    duplicate: bool = Field(description="True when the Idempotency-Key matched an earlier event")
    runs: list[RunRead]


class WebhookAccepted(Schema):
    run_id: uuid.UUID
    status: str
    duplicate: bool
