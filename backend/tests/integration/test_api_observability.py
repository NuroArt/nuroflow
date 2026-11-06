"""/metrics, SSE streams and the dashboard overview, over HTTP against real Postgres."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from nuroflow.api.container import AppContainer
from nuroflow.integrations.llm.base import LLMError
from tests.conftest import EngineHarness
from tests.integration.test_api import API, assert_problem, bearer, create_user, login
from tests.integration.test_scheduler import add_cron

pytestmark = pytest.mark.integration

OK_TASK = {"key": "a", "operator": "noop"}
AI_TASK = {
    "key": "ask",
    "operator": "ai.complete",
    "inputs": {"provider": "fake", "model": "m", "prompt": "hello"},
}


@pytest.fixture
async def editor(client: httpx.AsyncClient, container: AppContainer) -> dict[str, str]:
    await create_user(container, "editor@nuroworks.com")
    return bearer(await login(client, "editor@nuroworks.com"))


async def run_workflow(
    client: httpx.AsyncClient, headers: dict[str, str], slug: str, *tasks: dict[str, Any]
) -> dict[str, Any]:
    """Create a workflow and run it. The inline dispatcher finishes it before returning."""
    created = await client.post(
        f"{API}/workflows",
        headers=headers,
        json={"definition": {"slug": slug, "name": slug.title(), "tasks": list(tasks)}},
    )
    assert created.status_code == 201, created.text
    started = await client.post(
        f"{API}/workflows/{created.json()['id']}/runs", headers=headers, json={}
    )
    assert started.status_code == 202, started.text
    run: dict[str, Any] = (
        await client.get(f"{API}/runs/{started.json()['id']}", headers=headers)
    ).json()
    return run


def parse_sse(body: str) -> list[dict[str, Any]]:
    events = []
    for block in body.strip().split("\n\n"):
        fields: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            key, _, value = line.partition(": ")
            fields[key] = json.loads(value) if key == "data" else value
        if "event" in fields:
            events.append(fields)
    return events


# ============================================================================ /metrics
async def test_metrics_expose_http_red_metrics_by_route_template(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    run = await run_workflow(client, editor, "red", OK_TASK)
    await client.get(f"{API}/runs/{run['id']}", headers=editor)
    await client.get("/definitely/not/a/route/123")

    text = (await client.get("/metrics")).text
    assert 'route="/api/v1/runs/{run_id}",status="200"' in text
    assert 'route="unmatched",status="404"' in text
    assert run["id"] not in text  # raw IDs never become labels
    assert "nuroflow_http_request_duration_seconds_bucket" in text


async def test_metrics_token_is_enforced_when_configured(
    client: httpx.AsyncClient, container: AppContainer
) -> None:
    container.settings = container.settings.model_copy(
        update={"metrics_token": SecretStr("scrape-me")}
    )
    assert_problem(await client.get("/metrics"), 401, "authentication_failed")
    wrong = await client.get("/metrics", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    ok = await client.get("/metrics", headers={"Authorization": "Bearer scrape-me"})
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/plain")

    container.settings = container.settings.model_copy(update={"metrics_enabled": False})
    assert (await client.get("/metrics")).status_code == 404


def gauge(text: str, name: str, labels: str = "") -> float:
    match = re.search(rf"^{name}{re.escape(labels)} (\S+)$", text, re.MULTILINE)
    assert match, f"{name}{labels} not found"
    return float(match.group(1))


async def test_db_gauges_report_queue_depth_and_scheduler_lag(
    client: httpx.AsyncClient, harness: EngineHarness
) -> None:
    run_id = harness.start_run({"slug": "q", "name": "q", "tasks": [OK_TASK]}, dispatch=False)
    run, _ = harness.run(run_id)
    add_cron(harness, run.workflow_id, "* * * * *", minutes_overdue=5)

    text = (await client.get("/metrics")).text
    assert gauge(text, "nuroflow_queue_depth", '{queue="default"}') == 1
    assert gauge(text, "nuroflow_oldest_queued_task_age_seconds") >= 0
    assert gauge(text, "nuroflow_active_runs", '{status="pending"}') == 1
    assert 240 < gauge(text, "nuroflow_scheduler_lag_seconds") < 420


async def test_execution_metrics_count_attempts_and_finished_runs(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    def value(text: str, labels: str, name: str = "nuroflow_task_attempts_total") -> float:
        match = re.search(rf"^{name}{re.escape(labels)} (\S+)$", text, re.MULTILINE)
        return float(match.group(1)) if match else 0.0

    before = (await client.get("/metrics")).text
    await run_workflow(client, editor, "count", OK_TASK)
    after = (await client.get("/metrics")).text
    labels = '{operator="noop",outcome="succeeded"}'
    assert value(after, labels) - value(before, labels) == 1
    finished = '{status="succeeded"}'
    name = "nuroflow_runs_finished_total"
    assert value(after, finished, name) - value(before, finished, name) == 1


# ============================================================================ SSE
async def test_run_stream_sends_a_snapshot_then_ends_for_a_finished_run(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    run = await run_workflow(client, editor, "stream", OK_TASK)
    response = await client.get(f"{API}/runs/{run['id']}/stream", headers=editor)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    events = parse_sse(response.text)
    assert [e["event"] for e in events] == ["snapshot", "end"]
    snapshot = events[0]["data"]
    assert snapshot["status"] == "succeeded"
    assert [t["status"] for t in snapshot["tasks"]] == ["succeeded"]
    assert "output" not in snapshot["tasks"][0]  # status only: payloads stay out of streams


async def test_log_stream_sends_lines_with_ids_and_resumes_from_last_event_id(
    client: httpx.AsyncClient, editor: dict[str, str]
) -> None:
    run = await run_workflow(client, editor, "logs", AI_TASK)
    task_id = run["tasks"][0]["id"]
    url = f"{API}/task-runs/{task_id}/logs/stream"

    events = parse_sse((await client.get(url, headers=editor)).text)
    logs = [e for e in events if e["event"] == "log"]
    assert logs[0]["data"]["message"] == "llm call completed"
    assert [e["event"] for e in events][-2:] == ["status", "end"]
    assert events[-2]["data"] == {"status": "succeeded", "attempt_count": 1}

    resumed = parse_sse(
        (await client.get(url, headers=editor | {"Last-Event-ID": logs[-1]["id"]})).text
    )
    assert [e["event"] for e in resumed] == ["status", "end"]


async def test_streams_are_owner_scoped(
    client: httpx.AsyncClient, editor: dict[str, str], container: AppContainer
) -> None:
    run = await run_workflow(client, editor, "private", OK_TASK)
    await create_user(container, "other@nuroworks.com")
    other = bearer(await login(client, "other@nuroworks.com"))
    assert_problem(
        await client.get(f"{API}/runs/{run['id']}/stream", headers=other), 404, "not_found"
    )
    task_url = f"{API}/task-runs/{run['tasks'][0]['id']}/logs/stream"
    assert_problem(await client.get(task_url, headers=other), 404, "not_found")
    assert (await client.get(f"{API}/runs/{run['id']}/stream")).status_code == 401


# ============================================================================ overview
async def test_overview_aggregates_outcomes_durations_and_failures(
    client: httpx.AsyncClient, editor: dict[str, str], harness: EngineHarness
) -> None:
    await run_workflow(client, editor, "good-one", OK_TASK)
    await run_workflow(client, editor, "good-two", OK_TASK)
    harness.llm.queue(LLMError("invalid key", retryable=False))
    await run_workflow(client, editor, "broken", AI_TASK)

    data = (await client.get(f"{API}/stats/overview?hours=24", headers=editor)).json()
    assert data["runs"] == 3
    assert data["by_status"]["succeeded"] == 2
    assert data["by_status"]["failed"] == 1
    assert data["success_rate"] == pytest.approx(0.6667)
    assert data["duration_p50_seconds"] is not None
    assert data["bucket"] == "hour"
    assert len(data["series"]) in (24, 25)  # zero-filled hourly buckets
    assert sum(p["succeeded"] + p["failed"] for p in data["series"]) == 3
    assert data["failing_workflows"] == [
        {"workflow_id": data["failing_workflows"][0]["workflow_id"], "name": "Broken",
         "failed": 1, "total": 1}
    ]  # fmt: skip
    assert data["active_runs"] == 0

    weekly = (await client.get(f"{API}/stats/overview?hours=168", headers=editor)).json()
    assert weekly["bucket"] == "day"


async def test_overview_is_owner_scoped(
    client: httpx.AsyncClient, editor: dict[str, str], container: AppContainer
) -> None:
    await run_workflow(client, editor, "mine", OK_TASK)
    await create_user(container, "other@nuroworks.com")
    other = bearer(await login(client, "other@nuroworks.com"))
    data = (await client.get(f"{API}/stats/overview", headers=other)).json()
    assert data["runs"] == 0
    assert data["success_rate"] is None
