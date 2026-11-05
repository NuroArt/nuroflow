from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, SecretStr

from nuroflow.api.v1.schemas.common import Schema
from nuroflow.domain.enums import ChannelType, ConnectionType, DeliveryStatus, RunEvent

NAME = r"^[a-z0-9][a-z0-9_-]{0,99}$"


# ------------------------------------------------------------------ connections
class ConnectionCreate(Schema):
    name: str = Field(pattern=NAME, examples=["sales-bot"])
    type: ConnectionType
    # SecretStr: never echoed in validation errors or logs.
    secret: SecretStr = Field(description="API key or bot token. Write-only.", min_length=1)
    description: str | None = Field(default=None, max_length=500)
    config: dict[str, Any] = Field(default_factory=dict, examples=[{"base_url": "https://..."}])


class ConnectionUpdate(Schema):
    secret: SecretStr | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None, max_length=500)
    config: dict[str, Any] | None = None


class ConnectionRead(Schema):
    id: uuid.UUID
    name: str
    type: ConnectionType
    description: str | None
    config: dict[str, Any]
    has_secret: bool = True
    created_at: datetime
    updated_at: datetime


class ConnectionTestResult(Schema):
    ok: bool = True
    detail: str


class TelegramChatRead(Schema):
    chat_id: str
    type: str
    title: str


# ------------------------------------------------------------------ channels
class ChannelCreate(Schema):
    name: str = Field(pattern=NAME, examples=["ops-alerts"])
    type: ChannelType
    target: str = Field(
        min_length=1, max_length=500, description="Telegram chat_id / @channel, or https URL"
    )
    connection: str | None = Field(
        default=None,
        description="telegram: bot connection (else platform bot). webhook: http connection "
        "whose secret signs deliveries (X-NuroFlow-Signature).",
    )
    is_enabled: bool = True


class ChannelUpdate(Schema):
    target: str | None = Field(default=None, min_length=1, max_length=500)
    is_enabled: bool | None = None


class ChannelRead(Schema):
    id: uuid.UUID
    name: str
    type: ChannelType
    target: str
    connection_id: uuid.UUID | None
    is_enabled: bool
    created_at: datetime


class DeliveryRead(Schema):
    id: uuid.UUID
    channel_id: uuid.UUID
    workflow_run_id: uuid.UUID | None
    event: RunEvent
    status: DeliveryStatus
    attempt_count: int
    next_attempt_at: datetime | None
    last_error: str | None
    sent_at: datetime | None
    created_at: datetime


# ------------------------------------------------------------------ usage
class ModelUsage(Schema):
    provider: str | None
    model: str | None
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    priced_calls: int


class LLMUsage(Schema):
    since: datetime
    models: list[ModelUsage]
    total_cost_usd: float
    total_tokens: int
    unpriced_calls: int = Field(description="Calls to models with no configured price")
