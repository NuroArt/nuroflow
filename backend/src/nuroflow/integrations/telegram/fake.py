from __future__ import annotations

from nuroflow.integrations.telegram.base import TelegramChat, TelegramError, TelegramMessage


class FakeTelegramClient:
    """Records messages instead of sending them (tests, demos, CI)."""

    requires_credentials = False

    def __init__(self) -> None:
        self.sent: list[TelegramMessage] = []
        self.tokens: list[str | None] = []
        self.fail_next: list[TelegramError] = []

    def send_message(self, message: TelegramMessage, *, bot_token: str | None = None) -> int:
        if self.fail_next:
            raise self.fail_next.pop(0)
        self.sent.append(message)
        self.tokens.append(bot_token)
        return len(self.sent)

    def bot_username(self, *, bot_token: str | None = None) -> str:
        return "nuroflow_fake_bot"

    def recent_chats(self, *, bot_token: str | None = None) -> list[TelegramChat]:
        return [TelegramChat(chat_id="123456789", type="private", title="Fake User")]
