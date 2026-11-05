"""Connections (encrypted credentials), notification channels, deliveries and LLM usage.

Security model for connections:

* Secrets are **write-only**. They are encrypted with Fernet before the INSERT and are
  never returned by any endpoint. Responses only say ``has_secret: true``.
* Updating a connection may replace the secret but never reveals the old one.
* A connection still used by a notification channel can't be deleted (409), so
  alerts never silently stop.
* ``:test`` calls the provider with the stored secret and reports success or the
  provider's error message, which proves the key works without exposing it.

The adapters are synchronous (they also run in Celery workers), so the API calls them
via ``asyncio.to_thread`` to keep the event loop free.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from nuroflow.core.clock import Clock
from nuroflow.core.crypto import SecretBox
from nuroflow.core.exceptions import Conflict, NotFound, PermissionDenied, ValidationError
from nuroflow.core.ids import uuid7
from nuroflow.db.models import Connection, NotificationChannel, NotificationDelivery
from nuroflow.domain.auth import Principal, Scope
from nuroflow.domain.enums import ChannelType, ConnectionType
from nuroflow.integrations.llm.base import LLMError, LLMProvider, LLMRequest
from nuroflow.integrations.telegram.base import TelegramChat, TelegramClient, TelegramError
from nuroflow.notifications.delivery import DeliveryError, NotificationTransport
from nuroflow.repositories.interfaces import WorkflowUnitOfWork

logger = logging.getLogger(__name__)

# Cheapest model per provider for ``:test`` (a 1-token completion: fractions of a cent).
TEST_MODELS = {ConnectionType.OPENAI: "gpt-4o-mini", ConnectionType.ANTHROPIC: "claude-haiku-4-5"}
ALLOWED_CONFIG_KEYS = {
    ConnectionType.OPENAI: {"base_url", "test_model"},
    ConnectionType.ANTHROPIC: {"base_url", "test_model"},
    ConnectionType.TELEGRAM: set(),
    ConnectionType.HTTP: set(),
}


class IntegrationService:
    def __init__(
        self,
        uow: WorkflowUnitOfWork,
        *,
        secrets: SecretBox,
        clock: Clock,
        llm_providers: Mapping[str, LLMProvider],
        telegram: TelegramClient,
        transport: NotificationTransport,
    ) -> None:
        self._uow = uow
        self._repo = uow.integrations
        self._secrets = secrets
        self._clock = clock
        self._llm = llm_providers
        self._telegram = telegram
        self._transport = transport

    # ================================================================== connections
    async def list_connections(self, principal: Principal) -> Sequence[Connection]:
        _require(principal, Scope.WORKFLOWS_READ)
        return await self._repo.list_connections(principal.user_id)

    async def create_connection(
        self,
        principal: Principal,
        *,
        name: str,
        type: ConnectionType,
        secret: str,
        description: str | None,
        config: dict[str, Any],
    ) -> Connection:
        _require(principal, Scope.CONNECTIONS_WRITE)
        if await self._repo.get_connection_by_name(principal.user_id, name) is not None:
            raise Conflict(f"A connection named '{name}' already exists", field="name")
        connection = Connection(
            id=uuid7(),
            owner_id=principal.user_id,
            name=name,
            type=type,
            description=description,
            encrypted_secret=self._secrets.encrypt(secret),
            config=_valid_config(type, config),
        )
        await self._repo.add_connection(connection)
        await self._uow.commit()
        logger.info("connection created", extra={"connection_id": str(connection.id), "type": type})
        return connection

    async def update_connection(
        self,
        principal: Principal,
        connection_id: uuid.UUID,
        *,
        secret: str | None,
        description: str | None,
        config: dict[str, Any] | None,
    ) -> Connection:
        _require(principal, Scope.CONNECTIONS_WRITE)
        connection = await self._owned_connection(principal, connection_id)
        if secret is not None:
            connection.encrypted_secret = self._secrets.encrypt(secret)
        if description is not None:
            connection.description = description
        if config is not None:
            connection.config = _valid_config(connection.type, config)
        await self._uow.commit()
        return connection

    async def delete_connection(self, principal: Principal, connection_id: uuid.UUID) -> None:
        _require(principal, Scope.CONNECTIONS_WRITE)
        connection = await self._owned_connection(principal, connection_id)
        if await self._repo.connection_in_use(connection.id):
            raise Conflict("Connection is used by a notification channel; delete that first")
        await self._repo.delete_connection(connection)
        await self._uow.commit()

    async def test_connection(self, principal: Principal, connection_id: uuid.UUID) -> str:
        """Prove the stored secret works. Returns a short human-readable confirmation."""
        _require(principal, Scope.CONNECTIONS_WRITE)
        connection = await self._owned_connection(principal, connection_id)
        secret = self._secrets.decrypt(connection.encrypted_secret)
        match connection.type:
            case ConnectionType.TELEGRAM:
                try:
                    username = await asyncio.to_thread(
                        self._telegram.bot_username, bot_token=secret
                    )
                except TelegramError as exc:
                    raise ValidationError(f"Telegram rejected the token: {exc}") from exc
                return f"connected as @{username}"
            case ConnectionType.OPENAI | ConnectionType.ANTHROPIC:
                provider = self._llm[connection.type.value]
                model = connection.config.get("test_model") or TEST_MODELS[connection.type]
                request = LLMRequest(model=model, prompt="Reply with: ok", max_tokens=5)
                try:
                    response = await asyncio.to_thread(
                        provider.complete,
                        request,
                        api_key=secret,
                        base_url=connection.config.get("base_url"),
                    )
                except LLMError as exc:
                    raise ValidationError(
                        f"{connection.type.value} rejected the call: {exc}"
                    ) from exc
                return f"{response.provider}/{response.model} responded"
            case _:
                return "stored (http connections are used for webhook signing; nothing to call)"

    async def telegram_chats(
        self, principal: Principal, connection_id: uuid.UUID
    ) -> list[TelegramChat]:
        """Chats that recently messaged the bot: the friendly way to find a chat_id."""
        _require(principal, Scope.CONNECTIONS_WRITE)
        connection = await self._owned_connection(principal, connection_id)
        if connection.type is not ConnectionType.TELEGRAM:
            raise ValidationError("Only telegram connections have chats")
        secret = self._secrets.decrypt(connection.encrypted_secret)
        try:
            return await asyncio.to_thread(self._telegram.recent_chats, bot_token=secret)
        except TelegramError as exc:
            raise ValidationError(f"Telegram error: {exc}") from exc

    # ================================================================== channels
    async def list_channels(self, principal: Principal) -> Sequence[NotificationChannel]:
        _require(principal, Scope.WORKFLOWS_READ)
        return await self._repo.list_channels(principal.user_id)

    async def create_channel(
        self,
        principal: Principal,
        *,
        name: str,
        type: ChannelType,
        target: str,
        connection: str | None,
        is_enabled: bool,
    ) -> NotificationChannel:
        _require(principal, Scope.WORKFLOWS_WRITE)
        if (await self._repo.channels_by_name(principal.user_id, [name])).get(name):
            raise Conflict(f"A channel named '{name}' already exists", field="name")
        channel = NotificationChannel(
            id=uuid7(),
            owner_id=principal.user_id,
            name=name,
            type=type,
            target=_valid_target(type, target),
            connection_id=await self._channel_connection_id(principal, type, connection),
            is_enabled=is_enabled,
        )
        await self._repo.add_channel(channel)
        await self._uow.commit()
        return channel

    async def update_channel(
        self,
        principal: Principal,
        channel_id: uuid.UUID,
        *,
        target: str | None,
        is_enabled: bool | None,
    ) -> NotificationChannel:
        _require(principal, Scope.WORKFLOWS_WRITE)
        channel = await self._owned_channel(principal, channel_id)
        if target is not None:
            channel.target = _valid_target(channel.type, target)
        if is_enabled is not None:
            channel.is_enabled = is_enabled
        await self._uow.commit()
        return channel

    async def delete_channel(self, principal: Principal, channel_id: uuid.UUID) -> None:
        _require(principal, Scope.WORKFLOWS_WRITE)
        await self._repo.delete_channel(await self._owned_channel(principal, channel_id))
        await self._uow.commit()

    async def test_channel(self, principal: Principal, channel_id: uuid.UUID) -> None:
        """Send a test message right now, through the same transport as real alerts."""
        _require(principal, Scope.WORKFLOWS_WRITE)
        channel = await self._owned_channel(principal, channel_id)
        secret = None
        if channel.connection_id is not None:
            connection = await self._repo.get_connection(channel.connection_id)
            assert connection is not None
            secret = self._secrets.decrypt(connection.encrypted_secret)
        payload = {
            "event": "test",
            "channel": channel.name,
            "sent_at": self._clock.now().isoformat(),
        }
        try:
            await asyncio.to_thread(
                self._transport.send, channel.type, channel.target, secret, payload
            )
        except DeliveryError as exc:
            raise ValidationError(f"Test notification failed: {exc}") from exc

    async def deliveries(
        self, principal: Principal, *, channel_id: uuid.UUID | None, limit: int
    ) -> Sequence[NotificationDelivery]:
        _require(principal, Scope.WORKFLOWS_READ)
        return await self._repo.list_deliveries(
            principal.user_id, channel_id=channel_id, limit=limit
        )

    # ================================================================== usage
    async def llm_usage(self, principal: Principal, *, days: int) -> dict[str, Any]:
        _require(principal, Scope.RUNS_READ)
        since = self._clock.now() - timedelta(days=days)
        rows = await self._repo.llm_usage(principal.user_id, since=since)
        models = [
            {
                **row,
                "input_tokens": int(row["input_tokens"]),
                "output_tokens": int(row["output_tokens"]),
                "cost_usd": round(float(row["cost_usd"]), 6),
            }
            for row in rows
        ]
        return {
            "since": since,
            "models": models,
            "total_cost_usd": round(sum(m["cost_usd"] for m in models), 6),
            "total_tokens": sum(m["input_tokens"] + m["output_tokens"] for m in models),
            "unpriced_calls": sum(m["calls"] - m["priced_calls"] for m in models),
        }

    # ================================================================== helpers
    async def _owned_connection(self, principal: Principal, connection_id: uuid.UUID) -> Connection:
        connection = await self._repo.get_connection(connection_id)
        if connection is None or connection.owner_id != principal.user_id:
            # Strictly owner-only (no admin override): connections hold *someone's* keys.
            raise NotFound("Connection not found")
        return connection

    async def _owned_channel(
        self, principal: Principal, channel_id: uuid.UUID
    ) -> NotificationChannel:
        channel = await self._repo.get_channel(channel_id)
        if channel is None or channel.owner_id != principal.user_id:
            raise NotFound("Channel not found")
        return channel

    async def _channel_connection_id(
        self, principal: Principal, type: ChannelType, name: str | None
    ) -> uuid.UUID | None:
        if name is None:
            return None  # telegram: platform bot; webhook: unsigned
        connection = await self._repo.get_connection_by_name(principal.user_id, name)
        if connection is None:
            raise ValidationError(f"connection '{name}' not found", field="connection")
        expected = ConnectionType.TELEGRAM if type is ChannelType.TELEGRAM else ConnectionType.HTTP
        if connection.type is not expected:
            raise ValidationError(
                f"a {type.value} channel needs a {expected.value} connection, "
                f"'{name}' is {connection.type.value}",
                field="connection",
            )
        return connection.id


def _require(principal: Principal, scope: Scope) -> None:
    if not principal.has(scope):
        raise PermissionDenied(f"Missing required scope: {scope.value}", scope=scope.value)


def _valid_config(type: ConnectionType, config: dict[str, Any]) -> dict[str, Any]:
    unknown = set(config) - ALLOWED_CONFIG_KEYS[type]
    if unknown:
        raise ValidationError(
            f"unsupported config keys for {type.value}: {', '.join(sorted(unknown))}",
            field="config",
        )
    base_url = config.get("base_url")
    if base_url is not None and urlsplit(str(base_url)).scheme != "https":
        # The API key is sent to this URL: plain HTTP would leak it.
        raise ValidationError("base_url must use https", field="config.base_url")
    return config


def _valid_target(type: ChannelType, target: str) -> str:
    target = target.strip()
    if type is ChannelType.TELEGRAM:
        if not target.lstrip("-").isdigit() and not target.startswith("@"):
            raise ValidationError(
                "telegram target must be a numeric chat_id or @channelname", field="target"
            )
    elif urlsplit(target).scheme != "https":
        raise ValidationError("webhook target must be an https URL", field="target")
    return target
