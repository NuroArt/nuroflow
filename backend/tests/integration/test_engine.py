"""The workflow engine against real PostgreSQL: execution, retries, trigger rules, crash safety."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import yaml

from nuroflow.db.models import DataRecord, TaskAttempt, TaskLog, TaskRun, WorkflowRun
from nuroflow.domain.enums import RunStatus, TaskStatus
from nuroflow.engine.recovery import dispatch_due_retries
from tests.conftest import EngineHarness

pytestmark = pytest.mark.integration

EXAMPLE = Path(__file__).resolve().parents[3] / "examples/workflows/lead-qualification.yaml"
LEAD = {"name": "Ada Lovelace", "email": "ada@analytical.io", "company": "Engines Ltd"}
S, T = RunStatus, TaskStatus


def lead_workflow() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text())
    return data


def flow(*tasks: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"slug": f"t-{uuid.uuid4().hex[:8]}", "name": "test", "tasks": list(tasks), **extra}


def count(h: EngineHarness, model: type, **filters: Any) -> int:
    with h.session_factory() as s:
        stmt = sa.select(sa.func.count()).select_from(model)
        for key, value in filters.items():
            stmt = stmt.where(getattr(model, key) == value)
        return int(s.execute(stmt).scalar_one())


# --------------------------------------------------------------------------- the brief's example
def test_lead_qualification_workflow_end_to_end(harness: EngineHarness) -> None:
    harness.llm.queue({"score": 87, "tier": "hot", "reason": "Budget and urgency confirmed"})
    harness.llm.queue("Ada (Engines Ltd) is ready to buy this quarter.")

    run_id = harness.start_run(lead_workflow(), payload=LEAD)
    run, tasks = harness.run(run_id)

    assert run.status is S.SUCCEEDED
    assert {k: t.status for k, t in tasks.items()} == dict.fromkeys(
        ["qualify", "store", "summarize", "alert"], T.SUCCEEDED
    )
    assert run.started_at is not None
    assert run.finished_at is not None

    # Store in Database: native types survive templating ("{{ … score }}" → int 87).
    with harness.session_factory() as s:
        record = s.scalars(sa.select(DataRecord)).one()
    assert record.collection == "qualified_leads"
    assert record.record == {
        "email": LEAD["email"],
        "name": LEAD["name"],
        "score": 87,
        "tier": "hot",
    }

    # Telegram alert built from upstream outputs and params.
    message = harness.telegram.sent[0]
    assert message.chat_id == "123456789"
    assert "HOT lead (87/100)" in message.text
    assert "ready to buy" in message.text

    # The prompt treated lead data as data; the system prompt carries the injection guard.
    assert "untrusted" in (harness.llm.requests[0].system or "")
    assert '"email": "ada@analytical.io"' in harness.llm.requests[0].prompt

    # Error tracking and observability rows exist for every attempt.
    assert count(harness, TaskAttempt) == 4
    assert count(harness, TaskLog) >= 4
    assert tasks["qualify"].input is not None
    with harness.session_factory() as s:
        attempt = s.scalars(
            sa.select(TaskAttempt).where(TaskAttempt.task_run_id == tasks["qualify"].id)
        ).one()
    assert attempt.metrics["llm"]["provider"] == "fake"
    assert attempt.duration_ms is not None


# --------------------------------------------------------------------------- retries
def test_transient_failure_retries_with_backoff_then_succeeds(harness: EngineHarness) -> None:
    harness.llm.queue("this is not json", {"score": 40, "tier": "warm", "reason": "ok"})
    run_id = harness.start_run(lead_workflow(), payload=LEAD)

    run, tasks = harness.run(run_id)
    qualify = tasks["qualify"]
    assert run.status is S.RUNNING
    assert qualify.status is T.UP_FOR_RETRY
    assert qualify.attempt_count == 1
    assert qualify.next_attempt_at is not None
    assert "invalid JSON" in (qualify.error_message or "")
    delay = qualify.next_attempt_at - harness.clock.now()
    assert timedelta(seconds=5) <= delay <= timedelta(seconds=10)  # 10s base, equal jitter

    assert dispatch_due_retries(harness.session_factory, clock=harness.clock) == []  # not yet due
    harness.clock.advance(seconds=11)
    requests = dispatch_due_retries(harness.session_factory, clock=harness.clock)
    assert [r.task_run_id for r in requests] == [qualify.id]

    harness.dispatcher.enqueue(requests)
    run, tasks = harness.run(run_id)
    assert run.status is S.SUCCEEDED
    assert tasks["qualify"].attempt_count == 2
    assert count(harness, TaskAttempt, task_run_id=qualify.id) == 2


def test_permanent_failure_skips_downstream_and_fails_run(harness: EngineHarness) -> None:
    definition = lead_workflow()
    definition["tasks"][0]["inputs"]["provider"] = "not-configured"
    run_id = harness.start_run(definition, payload=LEAD)

    run, tasks = harness.run(run_id)
    assert run.status is S.FAILED
    assert run.error_message == "failed tasks: qualify"
    assert tasks["qualify"].status is T.FAILED
    assert tasks["qualify"].attempt_count == 1  # non-retryable: no retries burned
    assert {tasks[k].status for k in ("store", "summarize", "alert")} == {T.SKIPPED}
    with harness.session_factory() as s:
        attempt = s.scalars(sa.select(TaskAttempt)).one()
    assert attempt.error_type == "NonRetryableError"
    assert "Traceback" in (attempt.traceback or "")


def test_retries_exhausted_marks_failed(harness: EngineHarness) -> None:
    harness.llm.queue("bad", "bad")
    definition = flow(
        {
            "key": "ask",
            "operator": "ai.structured",
            "retries": 1,
            "retry_backoff_seconds": 0,
            "inputs": {
                "provider": "fake",
                "model": "m",
                "prompt": "p",
                "output_schema": {"type": "object"},
            },
        },
    )
    run_id = harness.start_run(definition)
    harness.dispatcher.enqueue(dispatch_due_retries(harness.session_factory, clock=harness.clock))
    run, tasks = harness.run(run_id)
    assert tasks["ask"].status is T.FAILED
    assert tasks["ask"].attempt_count == 2
    assert run.status is S.FAILED


def test_template_errors_fail_without_retry(harness: EngineHarness) -> None:
    definition = flow(
        {
            "key": "a",
            "operator": "transform.template",
            "retries": 3,
            "inputs": {"value": "{{ trigger.payload.missing_field }}"},
        },
    )
    _, tasks = harness.run(harness.start_run(definition))
    assert tasks["a"].status is T.FAILED
    assert tasks["a"].attempt_count == 1
    assert "missing_field" in (tasks["a"].error_message or "")


def test_oversized_output_fails_cleanly(harness: EngineHarness) -> None:
    """Outputs are stored in JSONB and fed to downstream templates, so they're capped
    (256 KB by default) instead of silently bloating the database."""
    definition = flow(
        {
            "key": "big",
            "operator": "transform.template",
            "retries": 2,
            "inputs": {"value": "{{ 'x' * 300000 }}"},
        },
    )
    _, tasks = harness.run(harness.start_run(definition))
    assert tasks["big"].status is T.FAILED
    assert tasks["big"].attempt_count == 1  # permanent: retrying produces the same output
    assert "limit is 262144" in (tasks["big"].error_message or "")


# --------------------------------------------------------------------------- trigger rules
def test_error_handler_and_cleanup_trigger_rules(harness: EngineHarness) -> None:
    definition = flow(
        {
            "key": "work",
            "operator": "control.delay",
            "inputs": {"seconds": 9999},
        },  # invalid → fails
        {"key": "next", "operator": "noop", "depends_on": ["work"]},
        {
            "key": "on_error",
            "operator": "noop",
            "depends_on": ["work"],
            "trigger_rule": "one_failed",
        },
        {
            "key": "cleanup",
            "operator": "noop",
            "depends_on": ["next", "on_error"],
            "trigger_rule": "all_done",
        },
    )
    run, tasks = harness.run(harness.start_run(definition))
    assert {k: t.status for k, t in tasks.items()} == {
        "work": T.FAILED,
        "next": T.SKIPPED,
        "on_error": T.SUCCEEDED,
        "cleanup": T.SUCCEEDED,
    }
    assert run.status is S.FAILED  # handling an error doesn't erase it


def test_diamond_dag_joins_after_both_branches(harness: EngineHarness) -> None:
    definition = flow(
        {"key": "start", "operator": "transform.template", "inputs": {"value": 2}},
        {
            "key": "left",
            "operator": "transform.template",
            "depends_on": ["start"],
            "inputs": {"value": "{{ tasks.start.output.value * 10 }}"},
        },
        {
            "key": "right",
            "operator": "transform.template",
            "depends_on": ["start"],
            "inputs": {"value": "{{ tasks.start.output.value + 1 }}"},
        },
        {
            "key": "join",
            "operator": "transform.template",
            "depends_on": ["left", "right"],
            "inputs": {"value": "{{ tasks.left.output.value + tasks.right.output.value }}"},
        },
    )
    run, tasks = harness.run(harness.start_run(definition))
    assert run.status is S.SUCCEEDED
    assert tasks["join"].output == {"value": 23}


# --------------------------------------------------------------------------- crash safety
def test_duplicate_delivery_is_a_no_op(harness: EngineHarness) -> None:
    run_id = harness.start_run(flow({"key": "a", "operator": "noop"}), dispatch=False)
    _, tasks = harness.run(run_id)
    first = harness.executor.execute(tasks["a"].id)
    second = harness.executor.execute(tasks["a"].id)  # redelivered message
    assert first == []
    assert second == []
    _, tasks = harness.run(run_id)
    assert tasks["a"].attempt_count == 1
    assert count(harness, TaskAttempt) == 1


def test_stale_result_from_reassigned_attempt_is_discarded(harness: EngineHarness) -> None:
    run_id = harness.start_run(flow({"key": "a", "operator": "noop", "retries": 2}), dispatch=False)
    _, tasks = harness.run(run_id)
    executor = harness.executor
    claim = executor._claim(tasks["a"].id)
    assert claim is not None
    outcome = executor._run_operator(claim)

    # Meanwhile the reaper gave the task to another worker (attempt 2 now owns it).
    with harness.session_factory() as s, s.begin():
        s.execute(sa.update(TaskRun).values(attempt_count=2).where(TaskRun.id == tasks["a"].id))

    assert executor._record(tasks["a"].id, claim, outcome) == []
    _, tasks = harness.run(run_id)
    assert tasks["a"].status is T.RUNNING  # untouched: the zombie result was dropped


def test_cancelled_run_does_not_execute_queued_tasks(harness: EngineHarness) -> None:
    run_id = harness.start_run(flow({"key": "a", "operator": "noop"}), dispatch=False)
    with harness.session_factory() as s, s.begin():
        s.execute(sa.update(WorkflowRun).values(status=S.CANCELLED).where(WorkflowRun.id == run_id))
    _, tasks = harness.run(run_id)
    assert harness.executor.execute(tasks["a"].id) == []
    _, tasks = harness.run(run_id)
    assert tasks["a"].status is T.CANCELLED
    assert count(harness, TaskAttempt) == 0


def test_concurrent_branch_completion_queues_join_exactly_once(harness: EngineHarness) -> None:
    """Two branches finish at the same moment on different workers. The run-row lock
    must ensure the join task is enqueued once, not twice."""
    definition = flow(
        {"key": "left", "operator": "control.delay", "inputs": {"seconds": 0.2}},
        {"key": "right", "operator": "control.delay", "inputs": {"seconds": 0.2}},
        {"key": "join", "operator": "noop", "depends_on": ["left", "right"]},
    )
    run_id = harness.start_run(definition, dispatch=False)
    _, tasks = harness.run(run_id)
    barrier = threading.Barrier(2)

    def work(key: str) -> list[Any]:
        barrier.wait()
        return harness.executor.execute(tasks[key].id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(work, ["left", "right"]))
    enqueued = [r.task_run_id for batch in results for r in batch]
    assert enqueued == [tasks["join"].id]


def test_data_store_is_idempotent_per_task_run(harness: EngineHarness) -> None:
    from nuroflow.engine.data_store import SqlDataStore

    harness.create_workflow(flow({"key": "a", "operator": "noop"}))  # creates the owner row
    run_id = harness.start_run(flow({"key": "a", "operator": "noop"}))
    _, tasks = harness.run(run_id)
    store = SqlDataStore(harness.session_factory)
    kwargs = dict(
        owner_id=harness.owner_id,
        collection="leads",
        workflow_run_id=run_id,
        task_run_id=tasks["a"].id,
    )
    first = store.upsert_for_task_run(record={"v": 1}, **kwargs)  # type: ignore[arg-type]
    second = store.upsert_for_task_run(record={"v": 2}, **kwargs)  # type: ignore[arg-type]
    assert first == second
    with harness.session_factory() as s:
        assert [r.record for r in s.scalars(sa.select(DataRecord))] == [{"v": 2}]


def test_heartbeat_updates_while_operator_runs(harness: EngineHarness) -> None:
    from nuroflow.engine.executor import TaskExecutor
    from nuroflow.engine.orchestrator import Orchestrator

    executor = TaskExecutor(
        session_factory=harness.session_factory,
        registry=harness.registry,
        orchestrator=Orchestrator(clock=harness.clock),
        clock=harness.clock,
        heartbeat_interval_seconds=0.05,
    )
    run_id = harness.start_run(
        flow({"key": "slow", "operator": "control.delay", "inputs": {"seconds": 0.3}}),
        dispatch=False,
    )
    _, tasks = harness.run(run_id)
    claim = executor._claim(tasks["slow"].id)
    assert claim is not None
    harness.clock.advance(seconds=42)  # the heartbeat must stamp the *clock's* time
    executor._run_operator(claim)
    _, tasks = harness.run(run_id)
    assert tasks["slow"].heartbeat_at == harness.clock.now()
