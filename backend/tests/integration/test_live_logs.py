"""Operator logs are persisted *while* the task runs (live tail), exactly once."""

from __future__ import annotations

import threading
import time

import pytest
import sqlalchemy as sa

from nuroflow.db.models import TaskLog
from nuroflow.domain.enums import TaskStatus
from nuroflow.engine.executor import TaskExecutor
from nuroflow.engine.orchestrator import Orchestrator
from tests.conftest import EngineHarness

pytestmark = pytest.mark.integration


def log_messages(h: EngineHarness) -> list[str]:
    with h.session_factory() as s:
        return list(s.scalars(sa.select(TaskLog.message).order_by(TaskLog.id)))


def test_logs_are_visible_mid_task_and_never_duplicated(harness: EngineHarness) -> None:
    executor = TaskExecutor(
        session_factory=harness.session_factory,
        registry=harness.registry,
        orchestrator=Orchestrator(clock=harness.clock),
        clock=harness.clock,
        heartbeat_interval_seconds=0.3,  # flush tick = min(0.3, 1.0)
    )
    definition = {
        "slug": "slow",
        "name": "slow",
        "tasks": [{"key": "wait", "operator": "control.delay", "inputs": {"seconds": 1.5}}],
    }
    run_id = harness.start_run(definition, dispatch=False)
    _, tasks = harness.run(run_id)
    worker = threading.Thread(target=executor.execute, args=(tasks["wait"].id,))
    worker.start()

    seen_mid_run: list[str] = []
    deadline = time.monotonic() + 1.2
    while time.monotonic() < deadline and not seen_mid_run:
        seen_mid_run = log_messages(harness)
        time.sleep(0.1)
    _, during = harness.run(run_id)
    worker.join(timeout=10)

    assert seen_mid_run == ["sleeping 1.5s"]
    assert during["wait"].status is TaskStatus.RUNNING  # really mid-task
    _, after = harness.run(run_id)
    assert after["wait"].status is TaskStatus.SUCCEEDED
    assert log_messages(harness) == ["sleeping 1.5s"]  # the final write skipped it
