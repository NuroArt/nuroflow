"""Workflows and runs over HTTP: the full stack, from YAML import to a finished run."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from nuroflow.api.container import AppContainer
from nuroflow.domain.enums import UserRole
from nuroflow.engine.dispatch import RecordingDispatcher
from tests.conftest import EngineHarness
from tests.integration.test_api import API, assert_problem, bearer, create_user, login

pytestmark = pytest.mark.integration

EXAMPLE = Path(__file__).resolve().parents[3] / "examples/workflows/lead-qualification.yaml"
LEAD = {"name": "Ada Lovelace", "email": "ada@analytical.io"}


def definition(slug: str = "demo", **extra: Any) -> dict[str, Any]:
    return {
        "slug": slug,
        "name": "Demo",
        "tasks": [
            {"key": "a", "operator": "transform.template", "inputs": {"value": "{{ params.n }}"}},
            {"key": "b", "operator": "noop", "depends_on": ["a"]},
        ],
        "params": {"n": 1},
        **extra,
    }


@pytest.fixture
async def editor(client: httpx.AsyncClient, container: AppContainer) -> dict[str, str]:
    await create_user(container, "editor@nuroworks.com")
    return bearer(await login(client, "editor@nuroworks.com"))


# --------------------------------------------------------------------------- definitions
async def test_import_yaml_and_run_lead_workflow_end_to_end(
    client: httpx.AsyncClient, editor: dict[str, str], harness: EngineHarness
) -> None:
    # The example routes run.failed alerts to a channel named ops-alerts.
    missing = await client.post(
        f"{API}/workflows:import",
        headers=editor | {"Content-Type": "application/yaml"},
        content=EXAMPLE.read_bytes(),
    )
    assert_problem(missing, 422, "validation_error")
    assert "ops-alerts" in missing.json()["detail"]
    channel = await client.post(
        f"{API}/channels",
        headers=editor,
        json={"name": "ops-alerts", "type": "telegram", "target": "-100123"},
    )
    assert channel.status_code == 201, channel.text

    imported = await client.post(
        f"{API}/workflows:import",
        headers=editor | {"Content-Type": "application/yaml"},
        content=EXAMPLE.read_bytes(),
    )
    assert imported.status_code == 201, imported.text
    workflow = imported.json()
    assert workflow["slug"] == "lead-qualification"
    assert workflow["current_version"]["version"] == 1

    harness.llm.queue({"score": 91, "tier": "hot", "reason": "Ready"}, "Ada wants a demo.")
    started = await client.post(
        f"{API}/workflows/{workflow['id']}/runs",
        headers=editor,
        json={"payload": LEAD, "params": {"sales_chat_id": "999"}},
    )
    assert started.status_code == 202
    run_id = started.json()["id"]

    detail = (await client.get(f"{API}/runs/{run_id}", headers=editor)).json()
    assert detail["status"] == "succeeded"
    assert [t["status"] for t in detail["tasks"]] == ["succeeded"] * 4
    assert detail["trigger_payload"] == LEAD
    assert harness.telegram.sent[0].chat_id == "999"

    qualify = next(t for t in detail["tasks"] if t["task_key"] == "qualify")
    attempts = (
        await client.get(f"{API}/task-runs/{qualify['id']}/attempts", headers=editor)
    ).json()
    assert attempts[0]["status"] == "succeeded"
    assert attempts[0]["metrics"]["llm"]["output_tokens"] >= 0
    logs = (await client.get(f"{API}/task-runs/{qualify['id']}/logs", headers=editor)).json()
    assert logs["items"][0]["message"] == "llm call completed"

    records = (await client.get(f"{API}/records/qualified_leads", headers=editor)).json()
    assert records["items"][0]["record"]["score"] == 91


async def test_validate_endpoint_reports_every_issue(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    good = await client.post(
        f"{API}/workflows:validate", headers=editor, json={"definition": definition()}
    )
    assert good.json() == {"valid": True, "issues": [], "task_order": ["a", "b"]}

    broken = definition()
    broken["tasks"][1]["operator"] = "telegram.send"
    broken["tasks"][1]["inputs"] = {"chat": 1}
    result = (
        await client.post(f"{API}/workflows:validate", headers=editor, json={"definition": broken})
    ).json()
    assert result["valid"] is False
    messages = " | ".join(i["message"] for i in result["issues"])
    assert "unknown input 'chat'" in messages
    assert "missing required input 'text'" in messages


async def test_create_rejects_invalid_definition_with_details(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    bad = definition()
    bad["tasks"][0]["depends_on"] = ["b"]
    response = await client.post(f"{API}/workflows", headers=editor, json={"definition": bad})
    body = assert_problem(response, 422, "validation_error")
    assert "dependency cycle" in str(body["errors"])


async def test_versioning_is_immutable_and_deduplicated(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    created = (
        await client.post(f"{API}/workflows", headers=editor, json={"definition": definition()})
    ).json()
    wf = created["id"]

    same = await client.post(
        f"{API}/workflows/{wf}/versions", headers=editor, json={"definition": definition()}
    )
    assert same.status_code == 200
    assert same.json()["created"] is False

    changed = definition(params={"n": 2})
    v2 = await client.post(
        f"{API}/workflows/{wf}/versions",
        headers=editor,
        json={"definition": changed, "change_note": "bump n"},
    )
    assert v2.status_code == 201
    assert v2.json()["version"]["version"] == 2

    renamed = await client.post(
        f"{API}/workflows/{wf}/versions",
        headers=editor,
        json={"definition": definition(slug="other")},
    )
    assert_problem(renamed, 422, "validation_error")

    versions = (await client.get(f"{API}/workflows/{wf}/versions", headers=editor)).json()
    assert [v["version"] for v in versions] == [2, 1]
    assert versions[0]["change_note"] == "bump n"


async def test_duplicate_slug_conflicts(client: httpx.AsyncClient, editor: dict[str, str]) -> None:
    await client.post(f"{API}/workflows", headers=editor, json={"definition": definition()})
    again = await client.post(f"{API}/workflows", headers=editor, json={"definition": definition()})
    assert_problem(again, 409, "conflict")


async def test_import_rejects_bad_yaml(client: httpx.AsyncClient, editor: dict[str, str]) -> None:
    headers = editor | {"Content-Type": "application/yaml"}
    for content in (b"key: [unclosed", b"- just\n- a list\n", b"!!python/object:os.system {}"):
        response = await client.post(f"{API}/workflows:import", headers=headers, content=content)
        assert_problem(response, 422, "validation_error")


# --------------------------------------------------------------------------- runs
async def test_idempotency_key_returns_the_same_run(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    wf = (
        await client.post(f"{API}/workflows", headers=editor, json={"definition": definition()})
    ).json()
    headers = editor | {"Idempotency-Key": "form-submission-123"}
    first = await client.post(f"{API}/workflows/{wf['id']}/runs", headers=headers, json={})
    second = await client.post(f"{API}/workflows/{wf['id']}/runs", headers=headers, json={})
    assert first.status_code == 202
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    listed = (
        await client.get(f"{API}/runs", headers=editor, params={"workflow_id": wf["id"]})
    ).json()
    assert len(listed["items"]) == 1


async def test_paused_workflow_refuses_runs(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    wf = (
        await client.post(f"{API}/workflows", headers=editor, json={"definition": definition()})
    ).json()
    paused = await client.post(
        f"{API}/workflows/{wf['id']}:pause", headers=editor, json={"paused": True}
    )
    assert paused.json()["is_paused"] is True
    response = await client.post(f"{API}/workflows/{wf['id']}/runs", headers=editor, json={})
    assert_problem(response, 409, "conflict")


async def test_max_active_runs_and_cancel(
    client: httpx.AsyncClient, editor: dict[str, str], container: AppContainer
) -> None:
    container.dispatcher = RecordingDispatcher()  # nothing executes: runs stay pending
    wf = (
        await client.post(
            f"{API}/workflows", headers=editor, json={"definition": definition(max_active_runs=1)}
        )
    ).json()
    run = (await client.post(f"{API}/workflows/{wf['id']}/runs", headers=editor, json={})).json()
    blocked = await client.post(f"{API}/workflows/{wf['id']}/runs", headers=editor, json={})
    assert_problem(blocked, 409, "conflict")

    cancelled = await client.post(f"{API}/runs/{run['id']}:cancel", headers=editor)
    assert cancelled.json()["status"] == "cancelled"
    detail = (await client.get(f"{API}/runs/{run['id']}", headers=editor)).json()
    assert {t["status"] for t in detail["tasks"]} == {"cancelled"}

    again = await client.post(f"{API}/runs/{run['id']}:cancel", headers=editor)
    assert_problem(again, 409, "conflict")
    # A slot freed up: a new run is accepted.
    assert (
        await client.post(f"{API}/workflows/{wf['id']}/runs", headers=editor, json={})
    ).status_code == 202


async def test_failed_run_exposes_error_details(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    bad = definition()
    bad["tasks"][0]["inputs"]["value"] = "{{ params.missing }}"
    wf = (await client.post(f"{API}/workflows", headers=editor, json={"definition": bad})).json()
    run = (await client.post(f"{API}/workflows/{wf['id']}/runs", headers=editor, json={})).json()
    detail = (await client.get(f"{API}/runs/{run['id']}", headers=editor)).json()
    assert detail["status"] == "failed"
    assert detail["error_message"] == "failed tasks: a"
    task_a = detail["tasks"][0]
    attempts = (await client.get(f"{API}/task-runs/{task_a['id']}/attempts", headers=editor)).json()
    assert attempts[0]["error_type"] == "TemplateRenderError"
    assert "missing" in attempts[0]["error_message"]


async def test_deleted_workflow_keeps_run_history(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    wf = (
        await client.post(f"{API}/workflows", headers=editor, json={"definition": definition()})
    ).json()
    run = (await client.post(f"{API}/workflows/{wf['id']}/runs", headers=editor, json={})).json()
    assert (await client.delete(f"{API}/workflows/{wf['id']}", headers=editor)).status_code == 204
    assert (await client.get(f"{API}/workflows/{wf['id']}", headers=editor)).status_code == 404
    assert (await client.get(f"{API}/runs/{run['id']}", headers=editor)).json()[
        "status"
    ] == "succeeded"
    # The slug is free again.
    recreated = await client.post(
        f"{API}/workflows", headers=editor, json={"definition": definition()}
    )
    assert recreated.status_code == 201


# --------------------------------------------------------------------------- access control
async def test_other_users_cannot_see_or_run_workflows(
    client: httpx.AsyncClient, editor: dict[str, str], container: AppContainer
) -> None:
    wf = (
        await client.post(f"{API}/workflows", headers=editor, json={"definition": definition()})
    ).json()
    run = (await client.post(f"{API}/workflows/{wf['id']}/runs", headers=editor, json={})).json()
    await create_user(container, "mallory@nuroworks.com")
    mallory = bearer(await login(client, "mallory@nuroworks.com"))

    assert (await client.get(f"{API}/workflows/{wf['id']}", headers=mallory)).status_code == 404
    assert (
        await client.post(f"{API}/workflows/{wf['id']}/runs", headers=mallory, json={})
    ).status_code == 404
    assert (await client.get(f"{API}/runs/{run['id']}", headers=mallory)).status_code == 404
    assert (await client.get(f"{API}/workflows", headers=mallory)).json()["items"] == []


async def test_viewer_can_read_but_not_write(
    client: httpx.AsyncClient, container: AppContainer
) -> None:
    await create_user(container, "viewer@nuroworks.com", UserRole.VIEWER)
    viewer = bearer(await login(client, "viewer@nuroworks.com"))
    assert (await client.get(f"{API}/workflows", headers=viewer)).status_code == 200
    denied = await client.post(
        f"{API}/workflows", headers=viewer, json={"definition": definition()}
    )
    assert_problem(denied, 403, "permission_denied")


async def test_api_key_with_runs_scope_can_trigger(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    wf = (
        await client.post(f"{API}/workflows", headers=editor, json={"definition": definition()})
    ).json()
    key = (
        await client.post(
            f"{API}/api-keys", headers=editor, json={"name": "zapier", "scopes": ["runs:write"]}
        )
    ).json()["key"]
    triggered = await client.post(
        f"{API}/workflows/{wf['id']}/runs", headers={"X-API-Key": key}, json={"payload": LEAD}
    )
    assert triggered.status_code == 202
    denied = await client.get(f"{API}/workflows", headers={"X-API-Key": key})
    assert_problem(denied, 403, "permission_denied")  # no workflows:read scope


async def test_operator_catalog(client: httpx.AsyncClient, editor: dict[str, str]) -> None:
    catalog = (await client.get(f"{API}/operators", headers=editor)).json()
    by_name = {op["name"]: op for op in catalog}
    assert by_name["ai.structured"]["default_queue"] == "ai"
    assert "output_schema" in by_name["ai.structured"]["input_schema"]["properties"]
