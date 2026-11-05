"""Connections, channels, the notification outbox and LLM usage, against real Postgres."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session, sessionmaker

from nuroflow.api.container import AppContainer
from nuroflow.core.crypto import SecretBox
from nuroflow.core.ids import uuid7
from nuroflow.db.models import (
    Connection,
    NotificationChannel,
    NotificationDelivery,
    NotificationSubscription,
)
from nuroflow.domain.enums import ChannelType, ConnectionType, DeliveryStatus, RunStatus, UserRole
from nuroflow.engine.dispatch import EnqueueRequest
from nuroflow.engine.orchestrator import Orchestrator
from nuroflow.engine.recovery import dispatch_due_retries
from nuroflow.integrations.credentials import CredentialError, SqlCredentialResolver
from nuroflow.integrations.llm.base import LLMError
from nuroflow.integrations.telegram.base import TelegramError
from nuroflow.notifications.delivery import (
    DeliveryClaim,
    DeliveryError,
    NotificationDeliverer,
    NotificationTransport,
)
from nuroflow.scheduler.jobs import SchedulerConfig, SchedulerJobs
from nuroflow.services.ingest import sign_webhook
from tests.conftest import EngineHarness
from tests.integration.test_api import API, assert_problem, bearer, create_user, login

pytestmark = pytest.mark.integration

BOT_TOKEN = "123456:AA-super-secret-token"


@pytest.fixture
async def editor(client: httpx.AsyncClient, container: AppContainer) -> dict[str, str]:
    await create_user(container, "editor@nuroworks.com")
    return bearer(await login(client, "editor@nuroworks.com"))


async def make_connection(
    client: httpx.AsyncClient, headers: dict[str, str], **body: Any
) -> dict[str, Any]:
    payload = {"name": "sales-bot", "type": "telegram", "secret": BOT_TOKEN} | body
    response = await client.post(f"{API}/connections", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    data: dict[str, Any] = response.json()
    return data


# ============================================================================ connections
async def test_secret_is_write_only_and_encrypted_at_rest(
    client: httpx.AsyncClient, editor: dict[str, str], container: AppContainer
) -> None:
    created = await make_connection(client, editor, description="Sales alerts bot")
    assert created["has_secret"] is True
    assert BOT_TOKEN not in json.dumps(created)
    listed = (await client.get(f"{API}/connections", headers=editor)).text
    assert BOT_TOKEN not in listed

    # Raw SQL: the column holds Fernet ciphertext, not the token.
    async with container.session_factory() as session:
        raw = await session.scalar(
            sa.text("SELECT encrypted_secret FROM connections WHERE id = :id"),
            {"id": created["id"]},
        )
    assert BOT_TOKEN.encode() not in raw
    assert container.secrets.decrypt(raw) == BOT_TOKEN


async def test_connection_names_are_unique_per_owner(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    await make_connection(client, editor)
    duplicate = await client.post(
        f"{API}/connections",
        headers=editor,
        json={"name": "sales-bot", "type": "openai", "secret": "sk"},
    )
    assert_problem(duplicate, 409, "conflict")


async def test_config_rejects_unknown_keys_and_plain_http_base_url(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    for config in ({"api_version": "x"}, {"base_url": "http://llm.example.com/v1"}):
        response = await client.post(
            f"{API}/connections",
            headers=editor,
            json={"name": "llm", "type": "openai", "secret": "sk", "config": config},
        )
        assert_problem(response, 422, "validation_error")


async def test_rotate_test_and_discover_chats(
    client: httpx.AsyncClient, editor: dict[str, str], harness: EngineHarness
) -> None:
    created = await make_connection(client, editor)
    url = f"{API}/connections/{created['id']}"
    rotated = await client.patch(url, headers=editor, json={"secret": "999:new-token"})
    assert rotated.status_code == 200
    assert "new-token" not in rotated.text

    tested = await client.post(f"{url}:test", headers=editor)
    assert tested.json() == {"ok": True, "detail": "connected as @nuroflow_fake_bot"}
    chats = (await client.get(f"{url}/telegram-chats", headers=editor)).json()
    assert chats == [{"chat_id": "123456789", "type": "private", "title": "Fake User"}]


async def test_llm_connection_test_reports_provider_errors(
    client: httpx.AsyncClient, editor: dict[str, str], harness: EngineHarness
) -> None:
    created = await make_connection(client, editor, name="claude", type="anthropic", secret="bad")
    harness.llm.queue(LLMError("Anthropic HTTP 401: invalid x-api-key", retryable=False))
    failed = await client.post(f"{API}/connections/{created['id']}:test", headers=editor)
    body = assert_problem(failed, 422, "validation_error")
    assert "invalid x-api-key" in str(body["detail"])
    ok = await client.post(f"{API}/connections/{created['id']}:test", headers=editor)
    assert ok.json()["ok"] is True


async def test_connections_are_private_and_need_connections_scope(
    client: httpx.AsyncClient, editor: dict[str, str], container: AppContainer
) -> None:
    created = await make_connection(client, editor)
    await create_user(container, "other@nuroworks.com")
    await create_user(container, "viewer@nuroworks.com", role=UserRole.VIEWER)
    await create_user(container, "admin@nuroworks.com", role=UserRole.ADMIN)
    other = bearer(await login(client, "other@nuroworks.com"))
    viewer = bearer(await login(client, "viewer@nuroworks.com"))
    admin = bearer(await login(client, "admin@nuroworks.com"))

    for headers in (other, admin):  # no admin override: these are someone's keys
        response = await client.post(f"{API}/connections/{created['id']}:test", headers=headers)
        assert_problem(response, 404, "not_found")
    forbidden = await client.post(
        f"{API}/connections", headers=viewer, json={"name": "x", "type": "openai", "secret": "s"}
    )
    assert_problem(forbidden, 403, "permission_denied")


async def test_connection_used_by_channel_cannot_be_deleted(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    created = await make_connection(client, editor)
    channel = await client.post(
        f"{API}/channels",
        headers=editor,
        json={"name": "ops", "type": "telegram", "target": "-100", "connection": "sales-bot"},
    )
    assert channel.status_code == 201
    blocked = await client.delete(f"{API}/connections/{created['id']}", headers=editor)
    assert_problem(blocked, 409, "conflict")
    await client.delete(f"{API}/channels/{channel.json()['id']}", headers=editor)
    deleted = await client.delete(f"{API}/connections/{created['id']}", headers=editor)
    assert deleted.status_code == 204


def test_sql_resolver_is_owner_scoped_and_type_checked(
    harness: EngineHarness, sync_sessions: sessionmaker[Session]
) -> None:
    harness.create_workflow({"slug": "x", "name": "x", "tasks": [{"key": "a", "operator": "noop"}]})
    box = SecretBox([Fernet.generate_key()])
    with sync_sessions() as s, s.begin():
        s.add(
            Connection(
                id=uuid7(),
                owner_id=harness.owner_id,
                name="claude",
                type=ConnectionType.ANTHROPIC,
                encrypted_secret=box.encrypt("sk-ant-owner"),
                config={"base_url": "https://proxy.example.com/v1"},
            )
        )
    resolver = SqlCredentialResolver(
        sync_sessions, secrets=box, defaults={ConnectionType.OPENAI: "sk-platform"}
    )
    cred = resolver.resolve(owner_id=harness.owner_id, kind=ConnectionType.ANTHROPIC, name="claude")
    assert (cred.secret, cred.source, cred.config["base_url"]) == (
        "sk-ant-owner",
        "connection",
        "https://proxy.example.com/v1",
    )
    assert "sk-ant-owner" not in repr(cred)
    with pytest.raises(CredentialError, match="not found"):  # another user's name
        resolver.resolve(owner_id=uuid.uuid4(), kind=ConnectionType.ANTHROPIC, name="claude")
    with pytest.raises(CredentialError, match="not openai"):
        resolver.resolve(owner_id=harness.owner_id, kind=ConnectionType.OPENAI, name="claude")
    platform = resolver.resolve(owner_id=harness.owner_id, kind=ConnectionType.OPENAI, name=None)
    assert platform.source == "platform"
    with pytest.raises(CredentialError, match="NUROFLOW_TELEGRAM_BOT_TOKEN"):
        resolver.resolve(owner_id=harness.owner_id, kind=ConnectionType.TELEGRAM, name=None)


# ============================================================================ channels
async def test_channel_validation(client: httpx.AsyncClient, editor: dict[str, str]) -> None:
    await make_connection(client, editor, name="openai-key", type="openai", secret="sk")
    cases = [
        {"name": "a", "type": "telegram", "target": "not-a-chat"},
        {"name": "b", "type": "webhook", "target": "http://insecure.example.com/hook"},
        {"name": "c", "type": "telegram", "target": "-100", "connection": "openai-key"},
        {"name": "d", "type": "telegram", "target": "-100", "connection": "missing"},
    ]
    for body in cases:
        response = await client.post(f"{API}/channels", headers=editor, json=body)
        assert_problem(response, 422, "validation_error")


async def test_channel_test_sends_through_the_real_transport(
    client: httpx.AsyncClient, editor: dict[str, str], harness: EngineHarness
) -> None:
    await make_connection(client, editor)
    channel = (
        await client.post(
            f"{API}/channels",
            headers=editor,
            json={"name": "ops", "type": "telegram", "target": "-100", "connection": "sales-bot"},
        )
    ).json()
    response = await client.post(f"{API}/channels/{channel['id']}:test", headers=editor)
    assert response.status_code == 204
    assert harness.telegram.sent[-1].chat_id == "-100"
    assert "test notification" in harness.telegram.sent[-1].text
    assert harness.telegram.tokens[-1] == BOT_TOKEN

    harness.telegram.fail_next.append(TelegramError("chat not found", retryable=False))
    failed = await client.post(f"{API}/channels/{channel['id']}:test", headers=editor)
    assert "chat not found" in str(assert_problem(failed, 422, "validation_error")["detail"])


# ============================================================================ outbox
FAILING: dict[str, Any] = {
    "key": "qualify",
    "operator": "ai.complete",
    "inputs": {"provider": "fake", "model": "m", "prompt": "p"},
    "retries": 1,
}


class InlineSender:
    def __init__(self, deliverer: NotificationDeliverer) -> None:
        self.deliverer = deliverer
        self.claims: list[DeliveryClaim] = []

    def send(self, claims: list[DeliveryClaim]) -> None:
        self.claims += claims
        for claim in claims:
            self.deliverer.deliver(claim)


def outbox(h: EngineHarness, *, max_attempts: int = 3) -> tuple[SchedulerJobs, InlineSender]:
    transport = NotificationTransport(
        telegram=h.telegram, clock=h.clock, public_base_url="https://flow.example.com"
    )
    deliverer = NotificationDeliverer(
        session_factory=h.session_factory,
        transport=transport,
        secrets=SecretBox([Fernet.generate_key()]),
        clock=h.clock,
        max_attempts=max_attempts,
    )
    sender = InlineSender(deliverer)
    jobs = SchedulerJobs(
        session_factory=h.session_factory,
        clock=h.clock,
        orchestrator=Orchestrator(clock=h.clock),
        queue_for=lambda name: h.registry.get(name).default_queue,
        config=SchedulerConfig(),
        notifier=sender,
    )
    return jobs, sender


def subscribe(h: EngineHarness, workflow_id: uuid.UUID, events: list[str]) -> uuid.UUID:
    with h.session_factory() as s, s.begin():
        channel = NotificationChannel(
            id=uuid7(), owner_id=h.owner_id, name="ops", type=ChannelType.TELEGRAM, target="-100"
        )
        s.add(channel)
        s.flush()
        s.add(
            NotificationSubscription(
                id=uuid7(), workflow_id=workflow_id, channel_id=channel.id, events=events
            )
        )
        return channel.id


def deliveries(h: EngineHarness) -> list[NotificationDelivery]:
    with h.session_factory() as s:
        return list(s.scalars(sa.select(NotificationDelivery).order_by(NotificationDelivery.id)))


def start_subscribed(
    h: EngineHarness, definition: dict[str, Any], events: list[str]
) -> tuple[uuid.UUID, dict[str, Any]]:
    """Plan a run without dispatching it, subscribe its workflow, return (run_id, tasks)."""
    run_id = h.start_run(definition, dispatch=False)
    run, tasks = h.run(run_id)
    subscribe(h, run.workflow_id, events)
    return run_id, tasks


def execute(h: EngineHarness, task_id: uuid.UUID, queue: str = "default") -> None:
    h.dispatcher.enqueue([EnqueueRequest(task_id, queue, 30)])


NOOP_FLOW = {"slug": "ok", "name": "ok", "tasks": [{"key": "a", "operator": "noop"}]}


def test_failed_run_queues_alerts_in_the_same_transaction(harness: EngineHarness) -> None:
    definition = {"slug": "leads", "name": "Leads <prod>", "tasks": [FAILING]}
    harness.llm.queue(LLMError("upstream 503", retryable=True), LLMError("bad", retryable=False))
    run_id, tasks = start_subscribed(
        harness, definition, ["run.started", "task.retrying", "run.failed"]
    )
    execute(harness, tasks["qualify"].id, "ai")  # attempt 1: retryable failure
    harness.clock.advance(minutes=10)
    retries = dispatch_due_retries(harness.session_factory, clock=harness.clock)
    harness.dispatcher.enqueue(retries)  # attempt 2: permanent failure

    run, _ = harness.run(run_id)
    assert run.status is RunStatus.FAILED
    rows = deliveries(harness)
    assert [d.event.value for d in rows] == ["run.started", "task.retrying", "run.failed"]
    assert rows[1].payload["task"] == {"key": "qualify", "attempt": 1}
    assert "upstream 503" in rows[1].payload["error"]

    jobs, sender = outbox(harness)
    jobs.deliver_notifications()
    assert [d.status for d in deliveries(harness)] == [DeliveryStatus.SENT] * 3
    assert len(sender.claims) == 3
    failed_alert = harness.telegram.sent[-1]
    assert failed_alert.chat_id == "-100"
    assert failed_alert.parse_mode == "HTML"
    assert "Leads &lt;prod&gt;" in failed_alert.text
    assert "run.failed" in failed_alert.text

    jobs.deliver_notifications()  # nothing left: no duplicates
    assert len(harness.telegram.sent) == 3


def test_only_subscribed_events_are_queued(harness: EngineHarness) -> None:
    definition = {"slug": "ok", "name": "ok", "tasks": [{"key": "a", "operator": "noop"}]}
    run_id = harness.start_run(definition, dispatch=False)
    run, tasks = harness.run(run_id)
    subscribe(harness, run.workflow_id, ["run.succeeded"])
    harness.dispatcher.enqueue([EnqueueRequest(tasks["a"].id, "default", 30)])
    assert [d.event.value for d in deliveries(harness)] == ["run.succeeded"]


def test_delivery_retries_with_backoff_then_gives_up(harness: EngineHarness) -> None:
    definition = {"slug": "ok", "name": "ok", "tasks": [{"key": "a", "operator": "noop"}]}
    run_id = harness.start_run(definition, dispatch=False)
    run, tasks = harness.run(run_id)
    subscribe(harness, run.workflow_id, ["run.succeeded"])
    harness.dispatcher.enqueue([EnqueueRequest(tasks["a"].id, "default", 30)])
    jobs, sender = outbox(harness, max_attempts=2)

    harness.telegram.fail_next.append(TelegramError("HTTP 502", retryable=True))
    jobs.deliver_notifications()
    (row,) = deliveries(harness)
    assert (row.status, row.attempt_count, row.last_error) == (
        DeliveryStatus.PENDING,
        1,
        "HTTP 502",
    )
    assert row.next_attempt_at is not None
    assert row.next_attempt_at > harness.clock.now()

    jobs.deliver_notifications()  # backoff not elapsed: nothing claimed
    assert len(sender.claims) == 1

    harness.clock.advance(minutes=20)
    harness.telegram.fail_next.append(TelegramError("HTTP 502", retryable=True))
    jobs.deliver_notifications()
    (row,) = deliveries(harness)
    assert (row.status, row.attempt_count) == (DeliveryStatus.FAILED, 2)


def test_expired_lease_is_reclaimed_and_stale_claims_are_fenced(harness: EngineHarness) -> None:
    definition = {"slug": "ok", "name": "ok", "tasks": [{"key": "a", "operator": "noop"}]}
    run_id = harness.start_run(definition, dispatch=False)
    run, tasks = harness.run(run_id)
    subscribe(harness, run.workflow_id, ["run.succeeded"])
    harness.dispatcher.enqueue([EnqueueRequest(tasks["a"].id, "default", 30)])

    class LostSender:  # the worker died: the message never ran
        def send(self, claims: list[DeliveryClaim]) -> None:
            self.claims = claims

    jobs, sender = outbox(harness)
    lost = LostSender()
    jobs._notifier = lost
    jobs.deliver_notifications()
    jobs.deliver_notifications()  # lease still held: not reclaimed
    assert len(lost.claims) == 1
    stale = lost.claims[0]

    harness.clock.advance(minutes=3)  # lease expired
    jobs._notifier = sender
    jobs.deliver_notifications()
    assert sender.claims[0].attempt == 2
    assert deliveries(harness)[0].status is DeliveryStatus.SENT
    # The original (lost) message finally runs: fenced off, no second send.
    assert sender.deliverer.deliver(stale) is None
    assert len(harness.telegram.sent) == 1


def test_webhook_channel_posts_signed_json(harness: EngineHarness) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    transport = NotificationTransport(
        telegram=harness.telegram,
        clock=harness.clock,
        public_base_url="https://flow.example.com",
        allow_private_networks=True,  # test host isn't resolvable; SSRF guard tested separately
        http_transport=httpx.MockTransport(handle),
    )
    payload = {"event": "run.failed", "run": {"id": "r1"}}
    transport.send(ChannelType.WEBHOOK, "https://hooks.example.com/x", "whsec_1", payload)
    request = seen[0]
    ts = request.headers["x-nuroflow-timestamp"]
    assert request.headers["x-nuroflow-signature"] == sign_webhook("whsec_1", ts, request.content)
    assert json.loads(request.content) == payload


def test_webhook_channel_refuses_private_addresses(harness: EngineHarness) -> None:
    transport = NotificationTransport(
        telegram=harness.telegram, clock=harness.clock, public_base_url="https://x"
    )
    with pytest.raises(DeliveryError, match="non-public") as info:
        transport.send(ChannelType.WEBHOOK, "https://127.0.0.1/hook", None, {})
    assert not info.value.retryable


# ============================================================================ API: spec sync, usage
async def test_spec_notifications_sync_and_usage_report(
    client: httpx.AsyncClient, editor: dict[str, str], harness: EngineHarness
) -> None:
    channel = await client.post(
        f"{API}/channels",
        headers=editor,
        json={"name": "ops-alerts", "type": "telegram", "target": "-100"},
    )
    assert channel.status_code == 201
    definition = {
        "slug": "usage",
        "name": "Usage",
        "tasks": [
            {
                "key": "ask",
                "operator": "ai.complete",
                "inputs": {"provider": "openai", "model": "gpt-4o-mini", "prompt": "x" * 400},
            }
        ],
        "notifications": [
            {"channel": "ops-alerts", "events": ["run.failed"]},
            {"channel": "ops-alerts", "events": ["run.succeeded"]},
        ],
    }
    created = await client.post(f"{API}/workflows", headers=editor, json={"definition": definition})
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    with harness.session_factory() as s:
        events = s.scalar(
            sa.select(NotificationSubscription.events).where(
                NotificationSubscription.workflow_id == uuid.UUID(workflow_id)
            )
        )
    assert events == ["run.failed", "run.succeeded"]

    started = await client.post(f"{API}/workflows/{workflow_id}/runs", headers=editor, json={})
    assert started.status_code == 202
    deliveries_page = (await client.get(f"{API}/notification-deliveries", headers=editor)).json()
    assert [d["event"] for d in deliveries_page] == ["run.succeeded"]

    usage = (await client.get(f"{API}/usage/llm", headers=editor)).json()
    (model,) = usage["models"]
    # The harness registers one fake under every provider name; it reports itself as "fake".
    assert (model["provider"], model["model"], model["calls"]) == ("fake", "gpt-4o-mini", 1)
    assert model["input_tokens"] == 100  # fake provider: len(prompt) // 4
    assert usage["unpriced_calls"] == 1
    assert usage["total_cost_usd"] == 0

    # Removing the notifications block removes the subscription on the next version.
    definition.pop("notifications")
    saved = await client.post(
        f"{API}/workflows/{workflow_id}/versions", headers=editor, json={"definition": definition}
    )
    assert saved.status_code in (200, 201), saved.text
    with harness.session_factory() as s:
        count = s.scalar(sa.select(sa.func.count()).select_from(NotificationSubscription))
    assert count == 0
