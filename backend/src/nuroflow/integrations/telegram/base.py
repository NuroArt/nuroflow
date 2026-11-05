"""Telegram port, implemented by ``bot_api.TelegramBotClient`` and ``fake.FakeTelegramClient``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    chat_id: str
    text: str
    parse_mode: str | None = None
    disable_notification: bool = False


class TelegramError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class TelegramChat:
    chat_id: str
    type: str
    title: str


class TelegramClient(Protocol):
    requires_credentials: bool

    def send_message(self, message: TelegramMessage, *, bot_token: str | None = None) -> int:
        """Send a message and return Telegram's message_id."""
        ...

    def bot_username(self, *, bot_token: str | None = None) -> str:
        """``getMe``: validates the token and identifies the bot."""
        ...

    def recent_chats(self, *, bot_token: str | None = None) -> list[TelegramChat]:
        """Chats that recently messaged the bot (``getUpdates``): how users find their chat_id."""
        ...
