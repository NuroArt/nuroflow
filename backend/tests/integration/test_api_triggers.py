"""Events, signed webhooks, trigger sync and backpressure over HTTP."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest

from nuroflow.api.container import AppContainer
from nuroflow.domain.enums import UserRole
from nuroflow.engine.dispatch import RecordingDispatcher
from nuroflow.services.ingest import sign_webhook
from tests.conftest import EngineHarness
from tests.integration.test_api import API, assert_problem, bearer, create_user, login

pytestmark = pytest.mark.integration

LEAD = {"name": "Ada", "email": "ada@x.io", "source": "website"}


def definition(slug: str = "leads", **extra: Any) -> dict[str, Any]:
    return {
        "slug": slug,
        "name": "Leads",
        "triggers": [
            {"type": "event", "event_type": "lead.captured", "event_filter": {"source": "website"}},
            {"type": "webhook", "name": "typeform"},
            {"type": "cron", "cron": "0 9 * * 1-5", "timezone": "America/New_York"},
        ],
        "tasks": [
            {
                "key": "greet",
                "operator": "transform.template",
                "inputs": {"value": "Hi {{ trigger.payload.name }}"},
            },
        ],
        **extra,
    }


@pytest.fixture
async def editor(client: httpx.AsyncClient, container: AppContainer) -> dict[str, str]:
    await create_user(container, "editor@nuroworks.com")
    return bearer(await login(client, "editor@nuroworks.com"))


async def create(client: httpx.AsyncClient, headers: dict[str, str], **kw: Any) -> dict[str, Any]:
    response = await client.post(
        f"{API}/workflows", headers=headers, json={"definition": definition(**kw)}
    )
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def triggers_by_type(
    client: httpx.AsyncClient, headers: dict[str, str], workflow_id: str
) -> dict[str, dict[str, Any]]:
    rows = (await client.get(f"{API}/workflows/{workflow_id}/triggers", headers=headers)).json()
    return {t["type"]: t for t in rows}


async def post_webhook(
    client: httpx.AsyncClient,
    trigger_id: str,
    secret: str,
    payload: dict[str, Any],
    *,
    timestamp: int | None = None,
    delivery: str | None = None,
    signature: str | None = None,
) -> httpx.Response:
    body = json.dumps(payload).encode()
    ts = str(timestamp if timestamp is not None else int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-NuroFlow-Timestamp": ts,
        "X-NuroFlow-Signature": signature or sign_webhook(secret, ts, body),
    }
    if delivery:
        headers["X-NuroFlow-Delivery"] = delivery
    return await client.post(f"{API}/hooks/{trigger_id}", content=body, headers=headers)


# --------------------------------------------------------------------------- trigger sync
async def test_spec_triggers_are_materialised(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    wf = await create(client, editor)
    triggers = await triggers_by_type(client, editor, wf["id"])
    assert set(triggers) == {"event", "webhook", "cron"}
    assert triggers["cron"]["next_fire_at"] is not None
    assert triggers["webhook"]["webhook_url"].endswith(f"/api/v1/hooks/{triggers['webhook']['id']}")
    assert triggers["event"]["event_filter"] == {"source": "website"}


async def test_new_versions_keep_webhook_secret_and_cron_position(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    wf = await create(client, editor)
    before = await triggers_by_type(client, editor, wf["id"])
    secret = (
        await client.get(f"{API}/triggers/{before['webhook']['id']}/secret", headers=editor)
    ).json()

    changed = definition()
    changed["tasks"][0]["inputs"]["value"] = "Hello {{ trigger.payload.name }}"
    changed["triggers"] = changed["triggers"][1:]  # drop the event trigger
    await client.post(
        f"{API}/workflows/{wf['id']}/versions", headers=editor, json={"definition": changed}
    )

    after = await triggers_by_type(client, editor, wf["id"])
    assert set(after) == {"webhook", "cron"}
    assert after["webhook"]["id"] == before["webhook"]["id"]
    assert after["cron"]["next_fire_at"] == before["cron"]["next_fire_at"]
    again = (
        await client.get(f"{API}/triggers/{after['webhook']['id']}/secret", headers=editor)
    ).json()
    assert again["secret"] == secret["secret"]


# --------------------------------------------------------------------------- events
async def test_event_fans_out_to_matching_workflows_and_runs(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    await create(client, editor, slug="crm-sync")
    await create(client, editor, slug="sales-alert")
    response = await client.post(
        f"{API}/events", headers=editor, json={"type": "lead.captured", "payload": LEAD}
    )
    assert response.status_code == 202
    accepted = response.json()
    assert len(accepted["runs"]) == 2
    for run in accepted["runs"]:
        detail = (await client.get(f"{API}/runs/{run['id']}", headers=editor)).json()
        assert detail["status"] == "succeeded"
        assert detail["trigger_type"] == "event"
        assert detail["tasks"][0]["output"] == {"value": "Hi Ada"}


async def test_event_filter_and_type_must_match(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    await create(client, editor)
    other_source = await client.post(
        f"{API}/events",
        headers=editor,
        json={"type": "lead.captured", "payload": {**LEAD, "source": "ads"}},
    )
    other_type = await client.post(
        f"{API}/events", headers=editor, json={"type": "lead.lost", "payload": LEAD}
    )
    assert other_source.json()["runs"] == []
    assert other_type.json()["runs"] == []


async def test_event_idempotency_key_prevents_duplicate_leads(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    await create(client, editor)
    headers = editor | {"Idempotency-Key": "typeform-response-77"}
    first = await client.post(
        f"{API}/events", headers=headers, json={"type": "lead.captured", "payload": LEAD}
    )
    retry = await client.post(
        f"{API}/events", headers=headers, json={"type": "lead.captured", "payload": LEAD}
    )
    assert first.status_code == 202
    assert retry.status_code == 200
    assert retry.json()["duplicate"] is True
    assert retry.json()["event_id"] == first.json()["event_id"]
    assert [r["id"] for r in retry.json()["runs"]] == [r["id"] for r in first.json()["runs"]]


async def test_events_need_events_write_scope(
    client: httpx.AsyncClient, container: AppContainer, editor: dict[str, str]
) -> None:
    await create_user(container, "viewer@nuroworks.com", UserRole.VIEWER)
    viewer = bearer(await login(client, "viewer@nuroworks.com"))
    denied = await client.post(f"{API}/events", headers=viewer, json={"type": "lead.captured"})
    assert_problem(denied, 403, "permission_denied")

    await create(client, editor)
    key = (
        await client.post(
            f"{API}/api-keys", headers=editor, json={"name": "form", "scopes": ["events:write"]}
        )
    ).json()["key"]
    via_key = await client.post(
        f"{API}/events", headers={"X-API-Key": key}, json={"type": "lead.captured", "payload": LEAD}
    )
    assert len(via_key.json()["runs"]) == 1


async def test_backpressure_queues_automated_runs_but_rejects_manual(
    client: httpx.AsyncClient, editor: dict[str, str], container: AppContainer
) -> None:
    container.dispatcher = RecordingDispatcher()  # runs stay active
    wf = await create(client, editor, max_active_runs=1)
    results = [
        (
            await client.post(
                f"{API}/events",
                headers=editor,
                json={"type": "lead.captured", "payload": {**LEAD, "n": i}},
            )
        ).json()["runs"][0]
        for i in range(3)
    ]
    assert [r["admitted_at"] is not None for r in results] == [True, False, False]
    assert all(r["status"] == "pending" for r in results)  # nothing dropped
    manual = await client.post(f"{API}/workflows/{wf['id']}/runs", headers=editor, json={})
    assert_problem(manual, 409, "conflict")


async def test_paused_workflows_ignore_events(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    wf = await create(client, editor)
    await client.post(f"{API}/workflows/{wf['id']}:pause", headers=editor, json={"paused": True})
    response = await client.post(
        f"{API}/events", headers=editor, json={"type": "lead.captured", "payload": LEAD}
    )
    assert response.json()["runs"] == []


# --------------------------------------------------------------------------- webhooks
async def test_signed_webhook_starts_a_run(
    client: httpx.AsyncClient, editor: dict[str, str], harness: EngineHarness
) -> None:
    wf = await create(client, editor)
    hook = (await triggers_by_type(client, editor, wf["id"]))["webhook"]
    secret = (await client.get(f"{API}/triggers/{hook['id']}/secret", headers=editor)).json()[
        "secret"
    ]
    assert secret.startswith("whsec_")

    response = await post_webhook(client, hook["id"], secret, {"name": "Grace"}, delivery="d-1")
    assert response.status_code == 202
    run = (await client.get(f"{API}/runs/{response.json()['run_id']}", headers=editor)).json()
    assert run["status"] == "succeeded"
    assert run["trigger_type"] == "webhook"
    assert run["tasks"][0]["output"] == {"value": "Hi Grace"}

    replay = await post_webhook(client, hook["id"], secret, {"name": "Grace"}, delivery="d-1")
    assert replay.status_code == 200
    assert replay.json() == {**response.json(), "duplicate": True, "status": "succeeded"}


@pytest.mark.parametrize(
    "attack",
    ["bad-signature", "stale-timestamp", "future-timestamp", "unknown-trigger", "missing-headers"],
)
async def test_webhook_rejects_forged_and_replayed_requests(
    client: httpx.AsyncClient, editor: dict[str, str], attack: str
) -> None:
    wf = await create(client, editor)
    hook = (await triggers_by_type(client, editor, wf["id"]))["webhook"]
    secret = (await client.get(f"{API}/triggers/{hook['id']}/secret", headers=editor)).json()[
        "secret"
    ]
    now = int(time.time())
    if attack == "bad-signature":
        response = await post_webhook(client, hook["id"], "whsec_wrong", {"x": 1})
    elif attack == "stale-timestamp":
        response = await post_webhook(client, hook["id"], secret, {"x": 1}, timestamp=now - 600)
    elif attack == "future-timestamp":
        response = await post_webhook(client, hook["id"], secret, {"x": 1}, timestamp=now + 600)
    elif attack == "unknown-trigger":
        response = await post_webhook(
            client, "01a1adb6-0000-7000-8000-000000000000", secret, {"x": 1}
        )
    else:
        response = await client.post(f"{API}/hooks/{hook['id']}", json={"x": 1})
    assert_problem(response, 401, "authentication_failed")


async def test_rotating_the_secret_invalidates_the_old_one(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    wf = await create(client, editor)
    hook = (await triggers_by_type(client, editor, wf["id"]))["webhook"]
    old = (await client.get(f"{API}/triggers/{hook['id']}/secret", headers=editor)).json()["secret"]
    new = (await client.post(f"{API}/triggers/{hook['id']}:rotate-secret", headers=editor)).json()[
        "secret"
    ]
    assert new != old
    assert (await post_webhook(client, hook["id"], old, {"a": 1})).status_code == 401
    assert (await post_webhook(client, hook["id"], new, {"a": 1})).status_code == 202


async def test_webhook_secrets_need_interactive_login(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    wf = await create(client, editor)
    hook = (await triggers_by_type(client, editor, wf["id"]))["webhook"]
    key = (
        await client.post(
            f"{API}/api-keys",
            headers=editor,
            json={"name": "ci", "scopes": ["workflows:read", "workflows:write"]},
        )
    ).json()["key"]
    denied = await client.get(f"{API}/triggers/{hook['id']}/secret", headers={"X-API-Key": key})
    assert_problem(denied, 403, "permission_denied")


async def test_paused_workflow_webhook_returns_409(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    wf = await create(client, editor)
    hook = (await triggers_by_type(client, editor, wf["id"]))["webhook"]
    secret = (await client.get(f"{API}/triggers/{hook['id']}/secret", headers=editor)).json()[
        "secret"
    ]
    await client.post(f"{API}/workflows/{wf['id']}:pause", headers=editor, json={"paused": True})
    assert_problem(await post_webhook(client, hook["id"], secret, {"a": 1}), 409, "conflict")
