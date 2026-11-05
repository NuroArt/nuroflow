"""``telegram.send``: send a message through the Telegram port."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from nuroflow.domain.enums import ConnectionType
from nuroflow.integrations.credentials import CredentialError, CredentialResolver
from nuroflow.integrations.telegram.base import TelegramClient, TelegramError, TelegramMessage
from nuroflow.operators.base import (
    NonRetryableError,
    Operator,
    OperatorContext,
    OperatorInput,
    RetryableError,
)


class TelegramSendInput(OperatorInput):
    chat_id: str | int
    text: str = Field(min_length=1, max_length=4096)  # Telegram's hard limit
    parse_mode: Literal["HTML", "MarkdownV2"] | None = None
    disable_notification: bool = False
    connection: str | None = Field(
        default=None,
        description="Telegram connection holding the bot token (else the platform bot).",
    )


class TelegramSendOperator(Operator):
    name = "telegram.send"
    description = "Send a Telegram message. Output: {message_id, chat_id}."
    input_model = TelegramSendInput
    default_queue = "notifications"

    def __init__(
        self, client: TelegramClient, *, credentials: CredentialResolver | None = None
    ) -> None:
        self._client = client
        self._credentials = credentials

    def execute(self, inputs: TelegramSendInput, ctx: OperatorContext) -> dict[str, Any]:
        message = TelegramMessage(
            chat_id=str(inputs.chat_id),
            text=inputs.text,
            parse_mode=inputs.parse_mode,
            disable_notification=inputs.disable_notification,
        )
        token = resolve_bot_token(self._client, self._credentials, ctx.owner_id, inputs.connection)
        try:
            message_id = self._client.send_message(message, bot_token=token)
        except TelegramError as exc:
            raise (RetryableError if exc.retryable else NonRetryableError)(str(exc)) from exc
        ctx.log("telegram message sent", chat_id=message.chat_id, message_id=message_id)
        return {"message_id": message_id, "chat_id": message.chat_id}


def resolve_bot_token(
    client: TelegramClient,
    credentials: CredentialResolver | None,
    owner_id: Any,
    connection: str | None,
) -> str | None:
    if not client.requires_credentials:
        return None
    if credentials is None:
        raise NonRetryableError("no credential resolver configured for telegram")
    try:
        return credentials.resolve(
            owner_id=owner_id, kind=ConnectionType.TELEGRAM, name=connection
        ).secret
    except CredentialError as exc:
        raise NonRetryableError(str(exc)) from exc
