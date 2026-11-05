"""Provider adapters, failover, cost metering and alert formatting.

Every HTTP adapter is tested against ``httpx.MockTransport``: we assert the exact
request we send (URL, headers, body) and how each provider response is classified.
No network, no keys, no cost, and it still catches real integration bugs.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable

import httpx
import pytest
from pydantic import ValidationError as PydanticValidationError

from nuroflow.core.config import Environment, Settings
from nuroflow.domain.enums import ConnectionType
from nuroflow.integrations.credentials import StaticCredentialResolver
from nuroflow.integrations.llm.anthropic import AnthropicProvider
from nuroflow.integrations.llm.base import LLMError, LLMRequest, LLMResponse
from nuroflow.integrations.llm.fake import FakeLLMProvider
from nuroflow.integrations.llm.openai import OpenAIProvider
from nuroflow.integrations.telegram.base import TelegramError, TelegramMessage
from nuroflow.integrations.telegram.bot_api import TelegramBotClient
from nuroflow.notifications.formatting import telegram_html
from nuroflow.operators.base import NonRetryableError, OperatorContext, RetryableError
from nuroflow.operators.builtin.ai import (
    AICompleteInput,
    AICompleteOperator,
    AIStructuredInput,
    AIStructuredOperator,
    estimate_cost,
)
from nuroflow.operators.builtin.telegram import TelegramSendInput, TelegramSendOperator

SCHEMA = {"type": "object", "properties": {"score": {"type": "integer"}}, "required": ["score"]}
Handler = Callable[[httpx.Request], httpx.Response]


def recorder(response: httpx.Response | Handler) -> tuple[list[httpx.Request], httpx.MockTransport]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response(request) if callable(response) else response

    return seen, httpx.MockTransport(handle)


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


# ============================================================================ OpenAI
def openai_ok(content: str = "hello") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "model": "gpt-4o-mini-2024-07-18",
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        },
    )


def test_openai_sends_bearer_key_system_prompt_and_json_schema() -> None:
    seen, transport = recorder(openai_ok('{"score": 7}'))
    provider = OpenAIProvider(transport=transport)
    response = provider.complete(
        LLMRequest(model="gpt-4o-mini", prompt="Rate", system="Be strict", json_schema=SCHEMA),
        api_key="sk-test",
    )
    request = seen[0]
    assert str(request.url) == "https://api.openai.com/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["messages"] == [
        {"role": "system", "content": "Be strict"},
        {"role": "user", "content": "Rate"},
    ]
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert (response.text, response.input_tokens, response.output_tokens) == ('{"score": 7}', 12, 3)
    assert response.model == "gpt-4o-mini-2024-07-18"


def test_openai_per_call_base_url_overrides_default() -> None:
    seen, transport = recorder(openai_ok())
    OpenAIProvider(transport=transport).complete(
        LLMRequest(model="m", prompt="p"), api_key="k", base_url="https://llm.internal.example/v1/"
    )
    assert str(seen[0].url) == "https://llm.internal.example/v1/chat/completions"


@pytest.mark.parametrize(
    ("status", "retryable"), [(429, True), (500, True), (503, True), (400, False), (401, False)]
)
def test_openai_error_classification(status: int, retryable: bool) -> None:
    _, transport = recorder(httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(LLMError) as info:
        OpenAIProvider(transport=transport).complete(LLMRequest(model="m", prompt="p"), api_key="k")
    assert info.value.retryable is retryable
    assert "nope" in str(info.value)


def test_openai_timeout_is_retryable_and_missing_key_is_not() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    _, transport = recorder(timeout)
    with pytest.raises(LLMError) as info:
        OpenAIProvider(transport=transport).complete(LLMRequest(model="m", prompt="p"), api_key="k")
    assert info.value.retryable
    with pytest.raises(LLMError) as missing:
        OpenAIProvider(transport=transport).complete(LLMRequest(model="m", prompt="p"))
    assert not missing.value.retryable


def test_openai_refusal_is_permanent() -> None:
    refusal = httpx.Response(
        200, json={"choices": [{"message": {"content": None, "refusal": "I can't help"}}]}
    )
    _, transport = recorder(refusal)
    with pytest.raises(LLMError) as info:
        OpenAIProvider(transport=transport).complete(LLMRequest(model="m", prompt="p"), api_key="k")
    assert not info.value.retryable


# ============================================================================ Anthropic
def test_anthropic_forces_a_tool_for_structured_output() -> None:
    seen, transport = recorder(
        httpx.Response(
            200,
            json={
                "id": "msg_1",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "tool_use", "name": "emit_output", "input": {"score": 9}}],
                "usage": {"input_tokens": 40, "output_tokens": 8},
                "stop_reason": "tool_use",
            },
        )
    )
    response = AnthropicProvider(transport=transport).complete(
        LLMRequest(model="claude-sonnet-4-5", prompt="Rate", system="Strict", json_schema=SCHEMA),
        api_key="sk-ant",
    )
    request = seen[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "sk-ant"
    assert request.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(request.content)
    assert body["system"] == "Strict"
    assert body["tools"][0]["input_schema"] == SCHEMA
    assert body["tool_choice"] == {"type": "tool", "name": "emit_output"}
    assert json.loads(response.text) == {"score": 9}
    assert (response.input_tokens, response.output_tokens) == (40, 8)


def test_anthropic_plain_text_joins_text_blocks() -> None:
    _, transport = recorder(
        httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "Hel"}, {"type": "text", "text": "lo"}]},
        )
    )
    response = AnthropicProvider(transport=transport).complete(
        LLMRequest(model="m", prompt="p"), api_key="k"
    )
    assert response.text == "Hello"


def test_anthropic_overloaded_529_is_retryable() -> None:
    _, transport = recorder(
        httpx.Response(529, json={"type": "error", "error": {"message": "Overloaded"}})
    )
    with pytest.raises(LLMError) as info:
        AnthropicProvider(transport=transport).complete(
            LLMRequest(model="m", prompt="p"), api_key="k"
        )
    assert info.value.retryable
    assert "Overloaded" in str(info.value)


def test_anthropic_missing_tool_call_is_retryable() -> None:
    _, transport = recorder(httpx.Response(200, json={"content": [{"type": "text", "text": "x"}]}))
    with pytest.raises(LLMError) as info:
        AnthropicProvider(transport=transport).complete(
            LLMRequest(model="m", prompt="p", json_schema=SCHEMA), api_key="k"
        )
    assert info.value.retryable


# ============================================================================ Telegram
def test_telegram_send_message_payload() -> None:
    seen, transport = recorder(httpx.Response(200, json={"ok": True, "result": {"message_id": 42}}))
    client = TelegramBotClient(transport=transport)
    message_id = client.send_message(
        TelegramMessage(chat_id="-100", text="<b>hi</b>", parse_mode="HTML"), bot_token="123:abc"
    )
    assert message_id == 42
    assert str(seen[0].url) == "https://api.telegram.org/bot123:abc/sendMessage"
    body = json.loads(seen[0].content)
    assert body["chat_id"] == "-100"
    assert body["parse_mode"] == "HTML"


def test_telegram_429_is_retryable_and_mentions_retry_after() -> None:
    _, transport = recorder(
        httpx.Response(
            429,
            json={
                "ok": False,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 7},
            },
        )
    )
    with pytest.raises(TelegramError) as info:
        TelegramBotClient(transport=transport).send_message(
            TelegramMessage(chat_id="1", text="x"), bot_token="t"
        )
    assert info.value.retryable
    assert "retry after 7s" in str(info.value)


def test_telegram_errors_never_leak_the_token() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    _, transport = recorder(refuse)
    with pytest.raises(TelegramError) as info:
        TelegramBotClient(transport=transport).send_message(
            TelegramMessage(chat_id="1", text="x"), bot_token="SECRET-TOKEN"
        )
    assert "SECRET-TOKEN" not in str(info.value)
    assert info.value.__cause__ is None  # chained exception would carry the URL


def test_telegram_bad_chat_is_permanent() -> None:
    _, transport = recorder(
        httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})
    )
    with pytest.raises(TelegramError) as info:
        TelegramBotClient(transport=transport).send_message(
            TelegramMessage(chat_id="1", text="x"), bot_token="t"
        )
    assert not info.value.retryable
    assert "chat not found" in str(info.value)


def test_telegram_recent_chats_deduplicates() -> None:
    updates = [
        {"message": {"chat": {"id": 5, "type": "private", "first_name": "Ada", "last_name": "L"}}},
        {"message": {"chat": {"id": 5, "type": "private", "first_name": "Ada", "last_name": "L"}}},
        {"channel_post": {"chat": {"id": -100, "type": "channel", "title": "Sales"}}},
    ]
    _, transport = recorder(httpx.Response(200, json={"ok": True, "result": updates}))
    chats = TelegramBotClient(transport=transport).recent_chats(bot_token="t")
    assert {(c.chat_id, c.title) for c in chats} == {("5", "Ada L"), ("-100", "Sales")}


# ============================================================================ operators
class ScriptedProvider:
    """A provider that requires credentials and records the key it was given."""

    requires_credentials = True

    def __init__(self, name: str, *outcomes: LLMResponse | LLMError) -> None:
        self.name = name
        self.outcomes = list(outcomes)
        self.keys: list[str | None] = []

    def complete(
        self, request: LLMRequest, *, api_key: str | None = None, base_url: str | None = None
    ) -> LLMResponse:
        self.keys.append(api_key)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, LLMError):
            raise outcome
        return outcome


def answer(provider: str, model: str, text: str = "ok", tokens: int = 1000) -> LLMResponse:
    return LLMResponse(
        text=text, provider=provider, model=model, input_tokens=tokens, output_tokens=tokens
    )


CREDS = StaticCredentialResolver(
    {
        (ConnectionType.OPENAI, None): "platform-openai",
        (ConnectionType.ANTHROPIC, "team-claude"): "team-anthropic",
    }
)


def test_ai_operator_resolves_named_connection_and_meters_cost() -> None:
    anthropic = ScriptedProvider("anthropic", answer("anthropic", "claude-sonnet-4-5"))
    op = AICompleteOperator(
        {"anthropic": anthropic},
        credentials=CREDS,
        pricing={"claude-sonnet-4-5": {"input": 3.0, "output": 15.0}},
    )
    c = ctx()
    out = op.execute(
        AICompleteInput(
            provider="anthropic", model="claude-sonnet-4-5", prompt="hi", connection="team-claude"
        ),
        c,
    )
    assert out["text"] == "ok"
    assert anthropic.keys == ["team-anthropic"]
    assert c.metrics["llm"]["cost_usd"] == pytest.approx(0.018)  # 1000*3/1M + 1000*15/1M


def test_ai_fallback_on_retryable_error() -> None:
    anthropic = ScriptedProvider("anthropic", LLMError("overloaded", retryable=True))
    openai = ScriptedProvider("openai", answer("openai", "gpt-4o-mini"))
    op = AICompleteOperator({"anthropic": anthropic, "openai": openai}, credentials=CREDS)
    c = ctx()
    out = op.execute(
        AICompleteInput.model_validate(
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-5",
                "connection": "team-claude",
                "prompt": "hi",
                "fallback": [{"provider": "openai", "model": "gpt-4o-mini"}],
            }
        ),
        c,
    )
    assert out["provider"] == "openai"
    assert openai.keys == ["platform-openai"]  # no connection named -> platform default
    assert c.metrics["llm"]["fallback_index"] == 1
    assert any("failing over" in line.message for line in c.logs)


def test_ai_does_not_fail_over_on_permanent_error() -> None:
    anthropic = ScriptedProvider("anthropic", LLMError("invalid x-api-key", retryable=False))
    openai = ScriptedProvider("openai", answer("openai", "gpt-4o-mini"))
    op = AICompleteOperator({"anthropic": anthropic, "openai": openai}, credentials=CREDS)
    with pytest.raises(NonRetryableError, match="invalid x-api-key"):
        op.execute(
            AICompleteInput.model_validate(
                {
                    "provider": "anthropic",
                    "model": "m",
                    "connection": "team-claude",
                    "prompt": "hi",
                    "fallback": [{"provider": "openai", "model": "gpt-4o-mini"}],
                }
            ),
            ctx(),
        )
    assert openai.keys == []


def test_ai_all_targets_retryable_failure_is_retryable() -> None:
    anthropic = ScriptedProvider("anthropic", LLMError("529", retryable=True))
    op = AICompleteOperator({"anthropic": anthropic}, credentials=CREDS)
    with pytest.raises(RetryableError):
        op.execute(
            AICompleteInput(provider="anthropic", model="m", prompt="p", connection="team-claude"),
            ctx(),
        )


def test_missing_credentials_fail_permanently_with_guidance() -> None:
    op = AICompleteOperator(
        {"anthropic": ScriptedProvider("anthropic")},
        credentials=StaticCredentialResolver({}),
    )
    with pytest.raises(NonRetryableError, match="no anthropic credentials"):
        op.execute(AICompleteInput(provider="anthropic", model="m", prompt="p"), ctx())


def test_simulated_providers_need_no_credentials() -> None:
    op = AIStructuredOperator({"anthropic": FakeLLMProvider("anthropic")})  # no resolver at all
    out = op.execute(
        AIStructuredInput(provider="anthropic", model="m", prompt="p", output_schema=SCHEMA), ctx()
    )
    assert out == {"score": 0}


def test_telegram_operator_resolves_bot_token() -> None:
    sent: list[str | None] = []

    class Client:
        requires_credentials = True

        def send_message(self, message: TelegramMessage, *, bot_token: str | None = None) -> int:
            sent.append(bot_token)
            return 1

    creds = StaticCredentialResolver({(ConnectionType.TELEGRAM, "sales-bot"): "123:abc"})
    op = TelegramSendOperator(Client(), credentials=creds)  # type: ignore[arg-type]
    op.execute(TelegramSendInput(chat_id=1, text="hi", connection="sales-bot"), ctx())
    assert sent == ["123:abc"]
    with pytest.raises(NonRetryableError):
        op.execute(TelegramSendInput(chat_id=1, text="hi", connection="nope"), ctx())


# ============================================================================ cost, format, config
@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-4o-mini-2024-07-18", 0.75),  # longest prefix (gpt-4o-mini) wins over gpt-4o
        ("gpt-4o-2024-08-06", 12.5),
        ("unknown-model", None),
    ],
)
def test_cost_uses_longest_matching_prefix(model: str, expected: float | None) -> None:
    pricing = {
        "gpt-4o": {"input": 2.5, "output": 10.0},
        "gpt-4o-mini": {"input": 0.15, "output": 0.6},
    }
    response = answer("openai", model, tokens=1_000_000)
    assert estimate_cost(pricing, response) == expected


def test_telegram_alert_escapes_untrusted_text() -> None:
    text = telegram_html(
        {
            "event": "run.failed",
            "workflow": {"name": "<script>Leads</script>"},
            "run": {"id": "r1", "status": "failed"},
            "error": "boom <b>&",
        },
        public_base_url="https://flow.example.com",
    )
    assert "<script>" not in text
    assert "&lt;script&gt;Leads" in text
    assert "boom &lt;b&gt;&amp;" in text
    assert "https://flow.example.com/api/v1/runs/r1" in text


def test_deployed_environments_must_use_live_integrations() -> None:
    with pytest.raises(PydanticValidationError, match="must be 'live'"):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            environment=Environment.PRODUCTION,
            secret_key="x" * 64,  # type: ignore[arg-type]
            llm_mode="simulated",
        )


def test_pricing_is_parsed_from_json_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NUROFLOW_LLM_PRICING", '{"gpt-4o-mini": {"input": 0.15, "output": 0.6}}')
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_pricing["gpt-4o-mini"]["output"] == 0.6
