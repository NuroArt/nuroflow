"""Operators in isolation: fakes for providers, httpx.MockTransport for HTTP."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from nuroflow.integrations.llm.base import LLMError
from nuroflow.integrations.llm.fake import FakeLLMProvider, fake_from_schema
from nuroflow.integrations.telegram.fake import FakeTelegramClient
from nuroflow.operators.base import NonRetryableError, OperatorContext, RetryableError
from nuroflow.operators.builtin.ai import AICompleteOperator, AIStructuredOperator
from nuroflow.operators.builtin.core import DelayOperator, TransformOperator
from nuroflow.operators.builtin.data import DataStoreOperator
from nuroflow.operators.builtin.http import HttpRequestOperator
from nuroflow.operators.builtin.telegram import TelegramSendOperator
from nuroflow.operators.registry import OperatorRegistry, UnknownOperatorError, build_registry
from nuroflow.services.workflows import validate_definition

SCHEMA = {
    "type": "object",
    "required": ["score", "tier"],
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "tier": {"type": "string", "enum": ["hot", "warm", "cold"]},
    },
}


@pytest.fixture
def ctx() -> OperatorContext:
    return OperatorContext(
        workflow_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        task_run_id=uuid.uuid4(),
        task_key="t",
        owner_id=uuid.uuid4(),
        attempt=1,
        timeout_seconds=30,
    )


@pytest.fixture
def llm() -> FakeLLMProvider:
    return FakeLLMProvider("anthropic")


# --------------------------------------------------------------------------- AI
def structured_inputs(**extra: Any) -> dict[str, Any]:
    return {
        "provider": "anthropic",
        "model": "m",
        "prompt": "score it",
        "output_schema": SCHEMA,
        **extra,
    }


def test_structured_returns_validated_object_and_usage(
    llm: FakeLLMProvider, ctx: OperatorContext
) -> None:
    llm.queue({"score": 87, "tier": "hot"})
    output = AIStructuredOperator({"anthropic": llm}).run(structured_inputs(), ctx)
    assert output == {"score": 87, "tier": "hot"}
    assert ctx.metrics["llm"]["provider"] == "anthropic"
    assert llm.requests[0].json_schema == SCHEMA


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ("not json", "invalid JSON"),
        (json.dumps({"score": 900, "tier": "hot"}), "failed schema validation"),
        (json.dumps({"score": 50}), "'tier' is a required property"),
    ],
)
def test_bad_model_answers_are_retryable(
    llm: FakeLLMProvider, ctx: OperatorContext, answer: str, message: str
) -> None:
    llm.queue(answer)
    with pytest.raises(RetryableError, match=message):
        AIStructuredOperator({"anthropic": llm}).run(structured_inputs(), ctx)


def test_code_fenced_json_is_accepted(llm: FakeLLMProvider, ctx: OperatorContext) -> None:
    llm.queue('```json\n{"score": 10, "tier": "cold"}\n```')
    assert AIStructuredOperator({"anthropic": llm}).run(structured_inputs(), ctx)["tier"] == "cold"


def test_unknown_provider_is_permanent(ctx: OperatorContext) -> None:
    with pytest.raises(NonRetryableError, match="not configured"):
        AICompleteOperator({}).run({"provider": "anthropic", "model": "m", "prompt": "x"}, ctx)


@pytest.mark.parametrize("retryable", [True, False])
def test_provider_errors_keep_their_retry_class(
    llm: FakeLLMProvider, ctx: OperatorContext, retryable: bool
) -> None:
    llm.queue(LLMError("upstream said no", retryable=retryable))
    expected = RetryableError if retryable else NonRetryableError
    with pytest.raises(expected):
        AICompleteOperator({"anthropic": llm}).run(
            {"provider": "anthropic", "model": "m", "prompt": "x"}, ctx
        )


def test_structured_static_check_validates_the_schema_itself() -> None:
    problems = AIStructuredOperator.static_check(structured_inputs(output_schema={"type": "nope"}))
    assert any("not a valid JSON Schema" in p for p in problems)


def test_fake_from_schema_satisfies_schema() -> None:
    assert fake_from_schema(SCHEMA) == {"score": 0, "tier": "hot"}


# --------------------------------------------------------------------------- HTTP
def http_op(handler: Any, *, resolver: Any = lambda host: ["93.184.216.34"]) -> HttpRequestOperator:
    return HttpRequestOperator(transport=httpx.MockTransport(handler), resolver=resolver)


def test_http_success_returns_json_and_sends_idempotency_key(ctx: OperatorContext) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"ok": True})

    out = http_op(handler).run(
        {"url": "https://api.example.com/x", "method": "POST", "json": {"a": 1}}, ctx
    )
    assert out["status_code"] == 200
    assert out["body"] == {"ok": True}
    assert seen["idempotency-key"] == ctx.idempotency_key


@pytest.mark.parametrize(
    ("status", "error"), [(503, RetryableError), (429, RetryableError), (404, NonRetryableError)]
)
def test_http_status_classification(ctx: OperatorContext, status: int, error: type) -> None:
    with pytest.raises(error, match=rf"HTTP {status}"):
        http_op(lambda r: httpx.Response(status)).run({"url": "https://api.example.com"}, ctx)


def test_http_timeouts_are_retryable(ctx: OperatorContext) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(RetryableError, match="timeout"):
        http_op(handler).run({"url": "https://api.example.com"}, ctx)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "192.168.1.1"])
def test_http_blocks_private_addresses(ctx: OperatorContext, ip: str) -> None:
    op = http_op(lambda r: httpx.Response(200), resolver=lambda host: [ip])
    with pytest.raises(NonRetryableError, match="non-public address"):
        op.run({"url": "http://metadata.internal/latest"}, ctx)


# --------------------------------------------------------------------------- misc operators
def test_telegram_send_uses_the_client(ctx: OperatorContext) -> None:
    client = FakeTelegramClient()
    out = TelegramSendOperator(client).run({"chat_id": 42, "text": "hi", "parse_mode": "HTML"}, ctx)
    assert out == {"message_id": 1, "chat_id": "42"}
    assert client.sent[0].text == "hi"


def test_data_store_passes_task_identity(ctx: OperatorContext) -> None:
    calls: list[dict[str, Any]] = []

    class Store:
        def upsert_for_task_run(self, **kwargs: Any) -> uuid.UUID:
            calls.append(kwargs)
            return uuid.UUID(int=1)

    out = DataStoreOperator(Store()).run({"collection": "leads", "record": {"a": 1}}, ctx)
    assert out["collection"] == "leads"
    assert calls[0]["task_run_id"] == ctx.task_run_id


def test_transform_and_delay(ctx: OperatorContext) -> None:
    slept: list[float] = []
    assert TransformOperator().run({"value": [1, 2]}, ctx) == {"value": [1, 2]}
    assert DelayOperator(sleep=slept.append).run({"seconds": 1.5}, ctx) == {"slept_seconds": 1.5}
    assert slept == [1.5]


def test_inputs_are_validated_after_rendering(ctx: OperatorContext) -> None:
    with pytest.raises(NonRetryableError, match=r"invalid inputs for control\.delay"):
        DelayOperator(sleep=lambda s: None).run({"seconds": 999}, ctx)


# --------------------------------------------------------------------------- registry & validation
@pytest.fixture
def registry(llm: FakeLLMProvider) -> OperatorRegistry:
    class NoStore:
        def upsert_for_task_run(self, **_: Any) -> uuid.UUID:
            raise AssertionError

    return build_registry(
        llm_providers={"anthropic": llm},
        telegram=FakeTelegramClient(),
        data_store=NoStore(),
        load_plugins=False,
    )


def test_registry_describes_every_operator(registry: OperatorRegistry) -> None:
    names = {d["name"] for d in registry.describe()}
    assert names == {
        "noop",
        "control.delay",
        "transform.template",
        "http.request",
        "data.store",
        "ai.complete",
        "ai.structured",
        "telegram.send",
    }
    with pytest.raises(UnknownOperatorError):
        registry.get("nope")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(registry.get("noop"))


def test_validate_definition_checks_operators_and_inputs(registry: OperatorRegistry) -> None:
    report = validate_definition(
        {
            "slug": "demo",
            "name": "Demo",
            "tasks": [
                {"key": "a", "operator": "does.not.exist"},
                {"key": "b", "operator": "telegram.send", "inputs": {"txt": "hi"}},
                {
                    "key": "c",
                    "operator": "http.request",
                    "inputs": {"url": "https://x.io", "json": {}},
                },
            ],
        },
        registry,
    )
    messages = [i.message for i in report.issues]
    assert not report.valid
    assert any("unknown operator 'does.not.exist'" in m for m in messages)
    assert any("unknown input 'txt'" in m for m in messages)
    assert any("missing required input 'chat_id'" in m for m in messages)
    assert not any(m.startswith("c:") for m in messages)  # `json` alias accepted
