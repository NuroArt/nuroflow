"""Builds provider adapters from settings: the single place that knows which concrete
LLM and Telegram clients exist.

Phase 3 wires **simulated** providers under the real names (``openai``, ``anthropic``)
plus ``fake``, so the reference workflow runs end to end with zero API keys and zero
cost. Phase 5 replaces the simulated entries with real SDK adapters whenever the
corresponding API key is configured. Operators and workflows don't change.
"""

from __future__ import annotations

import logging

from nuroflow.core.config import Settings
from nuroflow.integrations.llm.base import LLMProvider
from nuroflow.integrations.llm.fake import FakeLLMProvider
from nuroflow.integrations.telegram.base import TelegramClient
from nuroflow.integrations.telegram.fake import FakeTelegramClient

logger = logging.getLogger(__name__)


def build_llm_providers(settings: Settings) -> dict[str, LLMProvider]:
    providers: dict[str, LLMProvider] = {
        name: FakeLLMProvider(name) for name in ("fake", "openai", "anthropic")
    }
    logger.info("LLM providers are simulated (real adapters arrive in Phase 5)")
    return providers


def build_telegram_client(settings: Settings) -> TelegramClient:
    logger.info("Telegram is simulated (real Bot API client arrives in Phase 5)")
    return FakeTelegramClient()
