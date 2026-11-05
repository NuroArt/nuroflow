"""LLM provider port. OpenAI and Anthropic adapters implement it in Phase 5.

The engine and operators depend only on this Protocol (Dependency Inversion), so:

* tests and local demos run on :class:`~nuroflow.integrations.llm.fake.FakeLLMProvider`
  with zero API cost;
* switching a task from OpenAI to Anthropic is a one-word change in YAML;
* provider failover becomes a wrapper class, with no change to operators.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class LLMRequest:
    model: str
    prompt: str
    system: str | None = None
    max_tokens: int = 512
    temperature: float = 0.2
    # When set, the provider must return JSON matching this JSON Schema.
    json_schema: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class LLMError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMProvider(Protocol):
    name: str

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Run one completion. Raise :class:`LLMError` with ``retryable`` set appropriately."""
        ...
