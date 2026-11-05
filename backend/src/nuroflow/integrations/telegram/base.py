"""Telegram port. The real Bot API client arrives in Phase 5."""

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


class TelegramClient(Protocol):
    def send_message(self, message: TelegramMessage, *, bot_token: str | None = None) -> int:
        """Send a message and return Telegram's message_id."""
        ...
