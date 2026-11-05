"""OpenAI adapter: Chat Completions over plain httpx.

Why httpx instead of the ``openai`` SDK? One HTTP stack for every integration, one
timeout and error policy, trivial to fake with ``httpx.MockTransport``, and no SDK
version churn. Also works with any OpenAI-compatible server (Azure, vLLM, Ollama, …)
via the connection's ``base_url``.

Structured output uses ``response_format: json_schema``: the model is constrained to
the schema server-side, and ``ai.structured`` still validates afterwards (defence in depth).
"""

from __future__ import annotations

from typing import Any

import httpx

from nuroflow.integrations.http_errors import error_detail, is_retryable_status
from nuroflow.integrations.llm.base import LLMError, LLMRequest, LLMResponse


class OpenAIProvider:
    name = "openai"
    requires_credentials = True

    def __init__(
        self,
        *,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def complete(
        self, request: LLMRequest, *, api_key: str | None = None, base_url: str | None = None
    ) -> LLMResponse:
        if not api_key:
            raise LLMError("OpenAI API key missing", retryable=False)
        messages: list[dict[str, str]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})
        body: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_completion_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                # strict=False: strict mode rejects common schema features (minimum,
                # optional props). We validate ourselves afterwards anyway.
                "json_schema": {"name": "output", "schema": request.json_schema, "strict": False},
            }
        url = f"{(base_url or self._base_url).rstrip('/')}/chat/completions"
        try:
            response = self._client.post(
                url, json=body, headers={"Authorization": f"Bearer {api_key}"}
            )
        except httpx.TimeoutException as exc:
            raise LLMError("OpenAI request timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise LLMError(
                f"OpenAI connection error: {type(exc).__name__}", retryable=True
            ) from exc
        if response.status_code >= 400:
            raise LLMError(
                f"OpenAI HTTP {response.status_code}: {error_detail(response)}",
                retryable=is_retryable_status(response.status_code),
            )
        data = response.json()
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("OpenAI returned an unexpected payload", retryable=True) from exc
        if choice.get("message", {}).get("refusal"):
            raise LLMError(f"model refused: {choice['message']['refusal'][:200]}", retryable=False)
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            provider=self.name,
            model=data.get("model", request.model),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            raw={"id": data.get("id"), "finish_reason": choice.get("finish_reason")},
        )
