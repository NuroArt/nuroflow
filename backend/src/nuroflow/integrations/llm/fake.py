"""Deterministic LLM for tests, demos and CI. Costs nothing, never flakes.

* With scripted responses queued, it returns them in order (for testing retries,
  malformed JSON, and so on).
* Otherwise it fabricates a schema-valid object from ``json_schema``, or echoes the
  prompt for plain completions.
"""

from __future__ import annotations

import json
from collections import deque
from typing import Any

from nuroflow.integrations.llm.base import LLMError, LLMRequest, LLMResponse


class FakeLLMProvider:
    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.requests: list[LLMRequest] = []
        self._script: deque[str | LLMError] = deque()

    def queue(self, *responses: str | LLMError | dict[str, Any]) -> None:
        for r in responses:
            self._script.append(json.dumps(r) if isinstance(r, dict) else r)

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self._script:
            item = self._script.popleft()
            if isinstance(item, LLMError):
                raise item
            text = item
        elif request.json_schema is not None:
            text = json.dumps(fake_from_schema(request.json_schema))
        else:
            text = f"[{self.name}:{request.model}] {request.prompt[:200]}"
        return LLMResponse(
            text=text,
            provider=self.name,
            model=request.model,
            input_tokens=len(request.prompt) // 4,
            output_tokens=len(text) // 4,
        )


def fake_from_schema(schema: dict[str, Any]) -> Any:
    """Smallest value satisfying a (simple) JSON Schema: enums, min bounds, required keys."""
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    match schema.get("type"):
        case "object":
            props: dict[str, Any] = schema.get("properties", {})
            return {k: fake_from_schema(v) for k, v in props.items()}
        case "array":
            return [fake_from_schema(schema.get("items", {}))] * schema.get("minItems", 0)
        case "integer":
            return int(schema.get("minimum", 0))
        case "number":
            return float(schema.get("minimum", 0))
        case "boolean":
            return False
        case "null":
            return None
        case _:
            return "fake"
