"""The production transport: a real Celery worker consuming from real Redis.

Every other engine test uses inline dispatch. This one proves the actual wiring:
the CeleryDispatcher publishes ids → the worker claims → follow-ups are published by
the worker itself → the run completes across three different queues.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from celery.contrib.testing.worker import start_worker

from nuroflow.core.config import Settings
from nuroflow.domain.enums import RunStatus, TaskStatus
from nuroflow.engine.dispatch import CeleryDispatcher, EnqueueRequest
from nuroflow.workers import celery_app as worker_module
from nuroflow.workers.container import WorkerContainer, build_worker_container
from tests.conftest import EngineHarness

pytestmark = pytest.mark.integration


@pytest.fixture
def celery_worker(api_settings: Settings, harness: EngineHarness) -> Iterator[WorkerContainer]:
    app = worker_module.celery_app
    original_broker = app.conf.broker_url
    app.conf.broker_url = str(api_settings.redis_url).rsplit("/", 1)[0] + "/14"
    container = build_worker_container(
        api_settings,
        dispatcher=CeleryDispatcher(app),
        registry=harness.registry,  # same fakes the harness scripts and inspects
    )
    worker_module.set_container(container)
    try:
        with start_worker(
            app,
            pool="threads",
            concurrency=4,
            perform_ping_check=False,
            queues=["default", "ai", "notifications"],
            shutdown_timeout=10,
        ):
            yield container
    finally:
        worker_module.set_container(None)
        container.close()
        app.conf.broker_url = original_broker


def test_lead_workflow_runs_on_a_real_celery_worker(
    celery_worker: WorkerContainer, harness: EngineHarness
) -> None:
    from tests.integration.test_engine import LEAD, lead_workflow

    harness.llm.queue({"score": 77, "tier": "warm", "reason": "Good fit"}, "Follow up Tuesday.")
    run_id = harness.start_run(lead_workflow(), payload=LEAD, dispatch=False)
    _, tasks = harness.run(run_id)
    root = tasks["qualify"]
    assert root.queue == "ai"

    celery_worker.dispatcher.enqueue([EnqueueRequest(root.id, root.queue, timeout_seconds=60)])

    deadline = time.monotonic() + 30
    run, tasks = harness.run(run_id)
    while not run.status.is_terminal and time.monotonic() < deadline:
        time.sleep(0.2)
        run, tasks = harness.run(run_id)

    assert run.status is RunStatus.SUCCEEDED, {k: t.status for k, t in tasks.items()}
    assert {t.status for t in tasks.values()} == {TaskStatus.SUCCEEDED}
    assert {t.queue for t in tasks.values()} == {"ai", "default", "notifications"}
    assert all(t.worker_id for t in tasks.values())
    assert "WARM lead (77/100)" in harness.telegram.sent[0].text
