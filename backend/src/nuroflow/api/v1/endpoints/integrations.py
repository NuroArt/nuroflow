"""/connections, /channels, /notification-deliveries and /usage/llm."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from nuroflow.api.deps import CurrentPrincipal, IntegrationServiceDep
from nuroflow.api.v1.schemas.integrations import (
    ChannelCreate,
    ChannelRead,
    ChannelUpdate,
    ConnectionCreate,
    ConnectionRead,
    ConnectionTestResult,
    ConnectionUpdate,
    DeliveryRead,
    LLMUsage,
    TelegramChatRead,
)

router = APIRouter(tags=["integrations"])


# ------------------------------------------------------------------ connections
@router.get("/connections", response_model=list[ConnectionRead])
async def list_connections(
    principal: CurrentPrincipal, svc: IntegrationServiceDep
) -> list[ConnectionRead]:
    return [ConnectionRead.model_validate(c) for c in await svc.list_connections(principal)]


@router.post("/connections", status_code=status.HTTP_201_CREATED, response_model=ConnectionRead)
async def create_connection(
    body: ConnectionCreate, principal: CurrentPrincipal, svc: IntegrationServiceDep
) -> ConnectionRead:
    """Store a credential. The secret is encrypted at rest and **never returned**."""
    connection = await svc.create_connection(
        principal,
        name=body.name,
        type=body.type,
        secret=body.secret.get_secret_value(),
        description=body.description,
        config=body.config,
    )
    return ConnectionRead.model_validate(connection)


@router.patch("/connections/{connection_id}", response_model=ConnectionRead)
async def update_connection(
    connection_id: uuid.UUID,
    body: ConnectionUpdate,
    principal: CurrentPrincipal,
    svc: IntegrationServiceDep,
) -> ConnectionRead:
    """Rotate the secret and/or edit metadata. The old secret is overwritten, not returned."""
    connection = await svc.update_connection(
        principal,
        connection_id,
        secret=body.secret.get_secret_value() if body.secret else None,
        description=body.description,
        config=body.config,
    )
    return ConnectionRead.model_validate(connection)


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: uuid.UUID, principal: CurrentPrincipal, svc: IntegrationServiceDep
) -> None:
    await svc.delete_connection(principal, connection_id)


@router.post("/connections/{connection_id}:test", response_model=ConnectionTestResult)
async def test_connection(
    connection_id: uuid.UUID, principal: CurrentPrincipal, svc: IntegrationServiceDep
) -> ConnectionTestResult:
    """One tiny real call with the stored secret (422 with the provider's error on failure)."""
    return ConnectionTestResult(detail=await svc.test_connection(principal, connection_id))


@router.get("/connections/{connection_id}/telegram-chats", response_model=list[TelegramChatRead])
async def telegram_chats(
    connection_id: uuid.UUID, principal: CurrentPrincipal, svc: IntegrationServiceDep
) -> list[TelegramChatRead]:
    """Send any message to your bot, then call this to discover your ``chat_id``."""
    chats = await svc.telegram_chats(principal, connection_id)
    return [TelegramChatRead(chat_id=c.chat_id, type=c.type, title=c.title) for c in chats]


# ------------------------------------------------------------------ channels
@router.get("/channels", response_model=list[ChannelRead])
async def list_channels(
    principal: CurrentPrincipal, svc: IntegrationServiceDep
) -> list[ChannelRead]:
    return [ChannelRead.model_validate(c) for c in await svc.list_channels(principal)]


@router.post("/channels", status_code=status.HTTP_201_CREATED, response_model=ChannelRead)
async def create_channel(
    body: ChannelCreate, principal: CurrentPrincipal, svc: IntegrationServiceDep
) -> ChannelRead:
    """Workflows subscribe by name: ``notifications: [{channel: ops-alerts, events: [...]}]``."""
    channel = await svc.create_channel(
        principal,
        name=body.name,
        type=body.type,
        target=body.target,
        connection=body.connection,
        is_enabled=body.is_enabled,
    )
    return ChannelRead.model_validate(channel)


@router.patch("/channels/{channel_id}", response_model=ChannelRead)
async def update_channel(
    channel_id: uuid.UUID,
    body: ChannelUpdate,
    principal: CurrentPrincipal,
    svc: IntegrationServiceDep,
) -> ChannelRead:
    channel = await svc.update_channel(
        principal, channel_id, target=body.target, is_enabled=body.is_enabled
    )
    return ChannelRead.model_validate(channel)


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_id: uuid.UUID, principal: CurrentPrincipal, svc: IntegrationServiceDep
) -> None:
    await svc.delete_channel(principal, channel_id)


@router.post("/channels/{channel_id}:test", status_code=status.HTTP_204_NO_CONTENT)
async def test_channel(
    channel_id: uuid.UUID, principal: CurrentPrincipal, svc: IntegrationServiceDep
) -> None:
    """Send a test notification immediately (bypasses the outbox)."""
    await svc.test_channel(principal, channel_id)


@router.get("/notification-deliveries", response_model=list[DeliveryRead])
async def list_deliveries(
    principal: CurrentPrincipal,
    svc: IntegrationServiceDep,
    channel_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[DeliveryRead]:
    rows = await svc.deliveries(principal, channel_id=channel_id, limit=limit)
    return [DeliveryRead.model_validate(d) for d in rows]


# ------------------------------------------------------------------ usage
@router.get("/usage/llm", response_model=LLMUsage)
async def llm_usage(
    principal: CurrentPrincipal,
    svc: IntegrationServiceDep,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> LLMUsage:
    """Tokens and estimated cost per model (prices from ``NUROFLOW_LLM_PRICING``)."""
    return LLMUsage.model_validate(await svc.llm_usage(principal, days=days))
