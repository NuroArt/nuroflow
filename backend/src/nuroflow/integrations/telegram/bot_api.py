"""Telegram Bot API client over httpx.

The bot token lives in the URL path (``/bot<token>/sendMessage``), which is
Telegram's design. So we never log URLs here, and error messages never include them.
"""

from __future__ import annotations

from typing import Any

import httpx

from nuroflow.integrations.http_errors import error_detail, is_retryable_status
from nuroflow.integrations.telegram.base import TelegramChat, TelegramError, TelegramMessage


class TelegramBotClient:
    requires_credentials = True

    def __init__(
        self,
        *,
        base_url: str = "https://api.telegram.org",
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def _call(self, method: str, bot_token: str | None, payload: dict[str, Any]) -> Any:
        if not bot_token:
            raise TelegramError("Telegram bot token missing", retryable=False)
        try:
            response = self._client.post(f"{self._base_url}/bot{bot_token}/{method}", json=payload)
        except httpx.TimeoutException as exc:
            raise TelegramError(f"Telegram {method} timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            # str(exc) can contain the URL, and the URL contains the token.
            raise TelegramError(
                f"Telegram connection error: {type(exc).__name__}", retryable=True
            ) from None
        if response.status_code >= 400:
            detail = error_detail(response)
            if response.status_code == 429:
                retry_after = (response.json().get("parameters") or {}).get("retry_after")
                if retry_after:
                    detail += f" (retry after {retry_after}s)"
            raise TelegramError(
                f"Telegram {method} HTTP {response.status_code}: {detail}",
                retryable=is_retryable_status(response.status_code),
            )
        data = response.json()
        if not data.get("ok"):
            raise TelegramError(
                f"Telegram {method} failed: {error_detail(response)}", retryable=False
            )
        return data["result"]

    def send_message(self, message: TelegramMessage, *, bot_token: str | None = None) -> int:
        payload: dict[str, Any] = {
            "chat_id": message.chat_id,
            "text": message.text,
            "disable_notification": message.disable_notification,
            "link_preview_options": {"is_disabled": True},
        }
        if message.parse_mode:
            payload["parse_mode"] = message.parse_mode
        return int(self._call("sendMessage", bot_token, payload)["message_id"])

    def bot_username(self, *, bot_token: str | None = None) -> str:
        return str(self._call("getMe", bot_token, {})["username"])

    def recent_chats(self, *, bot_token: str | None = None) -> list[TelegramChat]:
        updates = self._call("getUpdates", bot_token, {"limit": 100, "timeout": 0})
        chats: dict[str, TelegramChat] = {}
        for update in updates:
            for key in ("message", "channel_post", "my_chat_member", "edited_message"):
                chat = (update.get(key) or {}).get("chat")
                if chat:
                    title = (
                        chat.get("title")
                        or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                        or chat.get("username", "")
                    )
                    chats[str(chat["id"])] = TelegramChat(
                        chat_id=str(chat["id"]), type=chat.get("type", ""), title=title
                    )
        return list(chats.values())
