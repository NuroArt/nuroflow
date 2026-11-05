"""``ai.complete`` and ``ai.structured``: LLM calls through the provider port.

``ai.structured`` is what makes AI safe to put in an automation. The model's answer
must parse as JSON **and** validate against the workflow's JSON Schema before any
downstream task sees it. A malformed answer is *retryable*: LLMs are stochastic, and
the next sample is usually fine.

Prompt-injection stance: lead data is untrusted. It can influence the model's
*answer*, but never which operators run, which connections are used, or who gets
messaged. Those come from the workflow definition, not from model output.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field

from nuroflow.domain.enums import ConnectionType, LogLevel
from nuroflow.integrations.credentials import CredentialError, CredentialResolver
from nuroflow.integrations.llm.base import LLMError, LLMProvider, LLMRequest, LLMResponse
from nuroflow.operators.base import (
    NonRetryableError,
    Operator,
    OperatorContext,
    OperatorInput,
    RetryableError,
)


class ProviderTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str = Field(min_length=1, max_length=100)
    connection: str | None = None


class AICompleteInput(OperatorInput):
    provider: str = Field(examples=["anthropic", "openai", "fake"])
    model: str = Field(min_length=1, max_length=100)
    connection: str | None = Field(
        default=None, description="Named connection holding the API key (else platform default)"
    )
    fallback: list[ProviderTarget] = Field(
        default_factory=list,
        max_length=3,
        description="Tried in order when the primary fails with a *retryable* error",
    )
    prompt: str = Field(min_length=1, max_length=100_000)
    system: str | None = Field(default=None, max_length=20_000)
    max_tokens: int = Field(default=512, ge=1, le=8192)
    temperature: float = Field(default=0.2, ge=0, le=2)


class AIStructuredInput(AICompleteInput):
    output_schema: dict[str, Any] = Field(description="JSON Schema the answer must satisfy")


class _AIOperator(Operator):
    default_queue = "ai"

    def __init__(
        self,
        providers: Mapping[str, LLMProvider],
        *,
        credentials: CredentialResolver | None = None,
        pricing: Mapping[str, Mapping[str, float]] | None = None,
    ) -> None:
        self._providers = providers
        self._credentials = credentials
        self._pricing = pricing or {}

    def _call(self, inputs: AICompleteInput, ctx: OperatorContext, **extra: Any) -> LLMResponse:
        targets = [
            ProviderTarget(
                provider=inputs.provider, model=inputs.model, connection=inputs.connection
            ),
            *inputs.fallback,
        ]
        last: LLMError | None = None
        for index, target in enumerate(targets):
            try:
                response = self._call_one(target, inputs, ctx, **extra)
            except LLMError as exc:
                last = exc
                if not exc.retryable:
                    # Permanent errors (bad key, bad request) are the author's to fix.
                    # Failing over would hide them behind a working-but-different model.
                    raise NonRetryableError(str(exc)) from exc
                if index + 1 < len(targets):
                    nxt = targets[index + 1]
                    ctx.log(
                        "llm provider failed, failing over",
                        level=LogLevel.WARNING,
                        failed=f"{target.provider}/{target.model}",
                        next=f"{nxt.provider}/{nxt.model}",
                        error=str(exc),
                    )
                continue
            self._meter(response, ctx, fallback_index=index)
            return response
        assert last is not None
        raise RetryableError(str(last)) from last

    def _call_one(
        self, target: ProviderTarget, inputs: AICompleteInput, ctx: OperatorContext, **extra: Any
    ) -> LLMResponse:
        provider = self._providers.get(target.provider)
        if provider is None:
            raise NonRetryableError(
                f"LLM provider '{target.provider}' is not configured "
                f"(available: {', '.join(sorted(self._providers)) or 'none'})"
            )
        api_key: str | None = None
        base_url: str | None = None
        if provider.requires_credentials:
            kind = ConnectionType(target.provider)
            if self._credentials is None:
                raise NonRetryableError(f"no credential resolver configured for {kind.value}")
            try:
                cred = self._credentials.resolve(
                    owner_id=ctx.owner_id, kind=kind, name=target.connection
                )
            except CredentialError as exc:
                raise NonRetryableError(str(exc)) from exc
            api_key, base_url = cred.secret, cred.config.get("base_url")
        request = LLMRequest(
            model=target.model,
            prompt=inputs.prompt,
            system=inputs.system,
            max_tokens=inputs.max_tokens,
            temperature=inputs.temperature,
            **extra,
        )
        return provider.complete(request, api_key=api_key, base_url=base_url)

    def _meter(self, response: LLMResponse, ctx: OperatorContext, *, fallback_index: int) -> None:
        usage: dict[str, Any] = {
            "provider": response.provider,
            "model": response.model,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "fallback_index": fallback_index,
        }
        cost = estimate_cost(self._pricing, response)
        if cost is not None:
            usage["cost_usd"] = cost
        ctx.metrics["llm"] = usage
        ctx.log("llm call completed", **usage)


class AICompleteOperator(_AIOperator):
    name = "ai.complete"
    description = "Generate text with an LLM. Output: {text, provider, model}."
    input_model = AICompleteInput

    def execute(self, inputs: AICompleteInput, ctx: OperatorContext) -> dict[str, Any]:
        response = self._call(inputs, ctx)
        return {
            "text": response.text.strip(),
            "provider": response.provider,
            "model": response.model,
        }


class AIStructuredOperator(_AIOperator):
    name = "ai.structured"
    description = "Get schema-validated JSON from an LLM. Output: the validated object."
    input_model = AIStructuredInput

    def execute(self, inputs: AIStructuredInput, ctx: OperatorContext) -> dict[str, Any]:
        validator = _validator(inputs.output_schema)
        response = self._call(inputs, ctx, json_schema=inputs.output_schema)
        try:
            data = json.loads(_strip_code_fence(response.text))
        except json.JSONDecodeError as exc:
            raise RetryableError(f"model returned invalid JSON: {exc.msg}") from exc
        errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
        if errors:
            detail = "; ".join(
                f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors[:5]
            )
            raise RetryableError(f"model output failed schema validation: {detail}")
        if not isinstance(data, dict):
            raise RetryableError("model output must be a JSON object")
        return data

    @classmethod
    def static_check(cls, inputs: dict[str, Any]) -> list[str]:
        problems = super().static_check(inputs)
        schema = inputs.get("output_schema")
        if isinstance(schema, dict):
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as exc:
                problems.append(f"output_schema is not a valid JSON Schema: {exc.message}")
            if schema.get("type") != "object":
                problems.append("output_schema must describe an object (type: object)")
        return problems


def estimate_cost(
    pricing: Mapping[str, Mapping[str, float]], response: LLMResponse
) -> float | None:
    """USD cost from configured per-1M-token prices. Longest matching model prefix
    wins, so ``gpt-4o-mini`` matches a dated ``gpt-4o-mini-2024-07-18`` response."""
    matches = [k for k in pricing if response.model.startswith(k)]
    if not matches:
        return None
    price = pricing[max(matches, key=len)]
    cost = (
        response.input_tokens * price.get("input", 0.0)
        + response.output_tokens * price.get("output", 0.0)
    ) / 1_000_000
    return round(cost, 6)


def _validator(schema: dict[str, Any]) -> Draft202012Validator:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise NonRetryableError(f"output_schema is not a valid JSON Schema: {exc.message}") from exc
    return Draft202012Validator(schema)


def _strip_code_fence(text: str) -> str:
    """Models sometimes wrap JSON in ```json fences despite instructions."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        stripped = stripped.rsplit("```", 1)[0]
    return stripped.strip()
