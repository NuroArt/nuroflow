from __future__ import annotations

from nuroflow.integrations.telegram.base import TelegramMessage


class FakeTelegramClient:
    """Records messages instead of sending them (tests, demos, CI)."""

    def __init__(self) -> None:
        self.sent: list[TelegramMessage] = []

    def send_message(self, message: TelegramMessage, *, bot_token: str | None = None) -> int:
        self.sent.append(message)
        return len(self.sent)
