"""Anthropic adapter: the Messages API over plain httpx.

Structured output uses the **forced tool** pattern: we declare one tool whose
``input_schema`` is the workflow's schema and set ``tool_choice`` to it, so the model
must answer by "calling" that tool with schema-shaped arguments. We return those
arguments as JSON text, and ``ai.structured`` validates them like any other answer.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from nuroflow.integrations.http_errors import error_detail, is_retryable_status
from nuroflow.integrations.llm.base import LLMError, LLMRequest, LLMResponse

ANTHROPIC_VERSION = "2023-06-01"
_TOOL = "emit_output"


class AnthropicProvider:
    name = "anthropic"
    requires_credentials = True

    def __init__(
        self,
        *,
        base_url: str = "https://api.anthropic.com/v1",
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def complete(
        self, request: LLMRequest, *, api_key: str | None = None, base_url: str | None = None
    ) -> LLMResponse:
        if not api_key:
            raise LLMError("Anthropic API key missing", retryable=False)
        body: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if request.system:
            body["system"] = request.system
        if request.json_schema is not None:
            body["tools"] = [
                {
                    "name": _TOOL,
                    "description": "Return the answer as structured data.",
                    "input_schema": request.json_schema,
                }
            ]
            body["tool_choice"] = {"type": "tool", "name": _TOOL}
        url = f"{(base_url or self._base_url).rstrip('/')}/messages"
        headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
        try:
            response = self._client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMError("Anthropic request timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise LLMError(
                f"Anthropic connection error: {type(exc).__name__}", retryable=True
            ) from exc
        if response.status_code >= 400:  # includes 529 "overloaded" (>= 500)
            raise LLMError(
                f"Anthropic HTTP {response.status_code}: {error_detail(response)}",
                retryable=is_retryable_status(response.status_code),
            )
        data = response.json()
        blocks = data.get("content") or []
        if request.json_schema is not None:
            tool = next((b for b in blocks if b.get("type") == "tool_use"), None)
            if tool is None:
                raise LLMError("Anthropic did not return the structured tool call", retryable=True)
            text = json.dumps(tool.get("input", {}))
        else:
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            provider=self.name,
            model=data.get("model", request.model),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            raw={"id": data.get("id"), "stop_reason": data.get("stop_reason")},
        )
