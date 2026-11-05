"""The single place that knows which concrete LLM and Telegram clients exist.

* ``llm_mode=live``: real OpenAI/Anthropic adapters (credentials resolved per call);
  ``fake`` stays available for smoke-test workflows.
* ``llm_mode=simulated``: every provider name maps to a deterministic fake. The
  reference workflow runs end to end with zero keys and zero cost (local dev, CI, demos).
"""

from __future__ import annotations

import logging

from nuroflow.core.config import Settings
from nuroflow.integrations.llm.anthropic import AnthropicProvider
from nuroflow.integrations.llm.base import LLMProvider
from nuroflow.integrations.llm.fake import FakeLLMProvider
from nuroflow.integrations.llm.openai import OpenAIProvider
from nuroflow.integrations.telegram.base import TelegramClient
from nuroflow.integrations.telegram.bot_api import TelegramBotClient
from nuroflow.integrations.telegram.fake import FakeTelegramClient

logger = logging.getLogger(__name__)


def build_llm_providers(settings: Settings) -> dict[str, LLMProvider]:
    if settings.llm_mode == "simulated":
        logger.warning("LLM providers are SIMULATED (NUROFLOW_LLM_MODE=simulated)")
        return {name: FakeLLMProvider(name) for name in ("fake", "openai", "anthropic")}
    return {
        "fake": FakeLLMProvider("fake"),
        "openai": OpenAIProvider(
            base_url=settings.openai_base_url, timeout=settings.llm_timeout_seconds
        ),
        "anthropic": AnthropicProvider(
            base_url=settings.anthropic_base_url, timeout=settings.llm_timeout_seconds
        ),
    }


def build_telegram_client(settings: Settings) -> TelegramClient:
    if settings.telegram_mode == "simulated":
        logger.warning("Telegram is SIMULATED (NUROFLOW_TELEGRAM_MODE=simulated)")
        return FakeTelegramClient()
    return TelegramBotClient(base_url=settings.telegram_base_url)
