"""Scheduler jobs on real PostgreSQL, including the Phase 4 chaos tests.

Exit criterion for Phase 4 (docs/ARCHITECTURE.md §14): kill a worker and lose
broker messages mid-run, and the run still completes.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from croniter import croniter

from nuroflow.db.models import TaskAttempt, Workflow, WorkflowRun, WorkflowTrigger
from nuroflow.domain.enums import AttemptStatus, RunStatus, TaskStatus, TriggerType
from nuroflow.domain.schedule import next_fire_after
from nuroflow.engine.dispatch import RecordingDispatcher, TaskDispatcher
from nuroflow.engine.orchestrator import Orchestrator
from nuroflow.scheduler.jobs import SchedulerConfig, SchedulerJobs
from nuroflow.scheduler.service import Scheduler
from tests.conftest import EngineHarness

pytestmark = pytest.mark.integration
S, T = RunStatus, TaskStatus
CONFIG = SchedulerConfig(
    batch_size=100,
    cron_max_catchup_runs=50,
    queued_timeout=timedelta(minutes=10),
    heartbeat_timeout=timedelta(seconds=90),
)


def make_scheduler(h: EngineHarness, dispatcher: TaskDispatcher | None = None) -> Scheduler:
    jobs = SchedulerJobs(
        session_factory=h.session_factory,
        clock=h.clock,
        orchestrator=Orchestrator(clock=h.clock),
        queue_for=lambda name: h.registry.get(name).default_queue,
        config=CONFIG,
    )
    return Scheduler(jobs=jobs, dispatcher=dispatcher or h.dispatcher, tick_seconds=1)


def flow(*tasks: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"slug": f"s-{uuid.uuid4().hex[:8]}", "name": "sched", "tasks": list(tasks), **extra}


NOOP = {"key": "a", "operator": "noop"}


def add_cron(
    h: EngineHarness,
    workflow_id: uuid.UUID,
    cron: str,
    *,
    minutes_overdue: float,
    catchup: bool = False,
) -> uuid.UUID:
    trigger_id = uuid.uuid4()
    with h.session_factory() as s, s.begin():
        s.add(
            WorkflowTrigger(
                id=trigger_id,
                workflow_id=workflow_id,
                type=TriggerType.CRON,
                spec_key=f"cron:{cron}:UTC",
                cron_expression=cron,
                timezone="UTC",
                catchup=catchup,
                # Like production, next_fire_at is a real slot: the latest one at or
                # before "now - overdue", i.e. a trigger that has been waiting that long.
                next_fire_at=croniter(
                    cron, h.clock.now() - timedelta(minutes=minutes_overdue, seconds=-1)
                ).get_prev(datetime),
            )
        )
    return trigger_id


def runs_of(h: EngineHarness, workflow_id: uuid.UUID) -> list[WorkflowRun]:
    with h.session_factory() as s:
        return list(
            s.scalars(
                sa.select(WorkflowRun)
                .where(WorkflowRun.workflow_id == workflow_id)
                .order_by(WorkflowRun.scheduled_for)
            )
        )


def trigger(h: EngineHarness, trigger_id: uuid.UUID) -> WorkflowTrigger:
    with h.session_factory() as s:
        row = s.get(WorkflowTrigger, trigger_id)
        assert row is not None
        return row


# --------------------------------------------------------------------------- cron
def test_cron_fires_once_per_slot_and_runs_execute(harness: EngineHarness) -> None:
    workflow_id, _ = harness.create_workflow(flow(NOOP))
    trigger_id = add_cron(harness, workflow_id, "*/5 * * * *", minutes_overdue=0.5)
    scheduler = make_scheduler(harness)

    report = scheduler.tick()
    assert report.by_job["fire_cron_triggers"] == 1
    runs = runs_of(harness, workflow_id)
    assert len(runs) == 1
    assert runs[0].status is S.SUCCEEDED
    assert runs[0].trigger_type.value == "cron"
    assert runs[0].trigger_payload["cron"] == "*/5 * * * *"
    assert runs[0].scheduled_for == trigger(harness, trigger_id).last_fired_at

    scheduler.tick()  # same instant: nothing new is due
    assert len(runs_of(harness, workflow_id)) == 1
    assert trigger(harness, trigger_id).next_fire_at > harness.clock.now()  # type: ignore[operator]


def test_without_catchup_a_long_outage_fires_only_the_latest_slot(harness: EngineHarness) -> None:
    workflow_id, _ = harness.create_workflow(flow(NOOP))
    trigger_id = add_cron(harness, workflow_id, "*/10 * * * *", minutes_overdue=55)
    make_scheduler(harness).tick()
    runs = runs_of(harness, workflow_id)
    assert len(runs) == 1
    latest_due = trigger(harness, trigger_id).last_fired_at
    assert latest_due is not None
    assert harness.clock.now() - latest_due < timedelta(minutes=10)


def align_clock(h: EngineHarness) -> None:
    """Move the frozen clock to the next hh:x5:00 so slot counts are deterministic."""
    now = h.clock.now()
    target = now.replace(second=0, microsecond=0) + timedelta(minutes=(5 - now.minute % 10) % 10)
    if target <= now:
        target += timedelta(minutes=10)
    h.clock.advance(seconds=(target - now).total_seconds())


def test_catchup_backfills_and_admission_respects_max_active_runs(harness: EngineHarness) -> None:
    align_clock(harness)  # deterministic slot count regardless of wall-clock time
    workflow_id, _ = harness.create_workflow(flow(NOOP, max_active_runs=2))
    add_cron(harness, workflow_id, "*/10 * * * *", minutes_overdue=45, catchup=True)
    recorder = RecordingDispatcher()  # hold execution so runs stay active
    scheduler = make_scheduler(harness, recorder)

    scheduler.tick()
    runs = runs_of(harness, workflow_id)
    assert len(runs) == 5  # every missed slot (x5-45m = :20 → :20 :30 :40 :50 :00), in order
    assert [r.scheduled_for for r in runs] == sorted(r.scheduled_for for r in runs)  # type: ignore[type-var]
    admitted = [r for r in runs if r.admitted_at is not None]
    assert len(admitted) == 2  # backpressure: the rest wait, nothing is dropped
    assert len(recorder.requests) == 2

    # Finish the admitted runs; the next tick admits the following two (oldest first).
    harness.dispatcher.enqueue(recorder.requests)
    recorder.requests.clear()
    scheduler.tick()
    runs = runs_of(harness, workflow_id)
    assert [r.admitted_at is not None for r in runs] == [True, True, True, True, False]


def test_paused_workflows_do_not_fire(harness: EngineHarness) -> None:
    workflow_id, _ = harness.create_workflow(flow(NOOP))
    add_cron(harness, workflow_id, "* * * * *", minutes_overdue=1)
    with harness.session_factory() as s, s.begin():
        s.execute(sa.update(Workflow).values(is_paused=True).where(Workflow.id == workflow_id))
    make_scheduler(harness).tick()
    assert runs_of(harness, workflow_id) == []


def test_two_schedulers_racing_never_double_fire(harness: EngineHarness) -> None:
    """HA without leader election: SKIP LOCKED + idempotency keys split the work."""
    workflow_ids = []
    for _ in range(15):
        wid, _ = harness.create_workflow(flow(NOOP))
        add_cron(harness, wid, "*/5 * * * *", minutes_overdue=1)
        workflow_ids.append(wid)
    a, b = (
        make_scheduler(harness, RecordingDispatcher()),
        make_scheduler(harness, RecordingDispatcher()),
    )
    barrier = threading.Barrier(2)

    def fire(scheduler: Scheduler) -> int:
        barrier.wait()
        return len(scheduler._jobs.fire_cron_triggers())

    with ThreadPoolExecutor(2) as pool:
        dispatched = sum(pool.map(fire, [a, b]))
    assert dispatched == 15
    assert all(len(runs_of(harness, wid)) == 1 for wid in workflow_ids)


# --------------------------------------------------------------------------- chaos: dead worker
def test_chaos_worker_killed_mid_task_run_still_completes(harness: EngineHarness) -> None:
    run_id = harness.start_run(
        flow(
            {**NOOP, "retries": 2},
        ),
        dispatch=False,
    )
    _, tasks = harness.run(run_id)
    # A worker claims the task... and is OOM-killed before reporting back.
    assert harness.executor._claim(tasks["a"].id) is not None
    _, tasks = harness.run(run_id)
    assert tasks["a"].status is T.RUNNING

    scheduler = make_scheduler(harness)
    harness.clock.advance(seconds=30)
    scheduler.tick()  # heartbeat only 30s old: healthy, left alone
    assert harness.run(run_id)[1]["a"].status is T.RUNNING

    harness.clock.advance(seconds=90)
    report = scheduler.tick()  # reaped → retried in the same tick → completes inline
    assert report.by_job["dispatch_retries"] == 1
    run, tasks = harness.run(run_id)
    assert run.status is S.SUCCEEDED
    assert tasks["a"].attempt_count == 2
    with harness.session_factory() as s:
        attempts = s.scalars(sa.select(TaskAttempt).order_by(TaskAttempt.attempt_number)).all()
    assert [(a.status, a.error_type) for a in attempts] == [
        (AttemptStatus.FAILED, "WorkerLostError"),
        (AttemptStatus.SUCCEEDED, None),
    ]


def test_chaos_dead_worker_without_retries_fails_the_run(harness: EngineHarness) -> None:
    run_id = harness.start_run(
        flow(NOOP, {"key": "b", "operator": "noop", "depends_on": ["a"]}), dispatch=False
    )
    _, tasks = harness.run(run_id)
    harness.executor._claim(tasks["a"].id)
    harness.clock.advance(minutes=5)
    make_scheduler(harness).tick()
    run, tasks = harness.run(run_id)
    assert tasks["a"].status is T.FAILED
    assert "worker lost" in (tasks["a"].error_message or "")
    assert tasks["b"].status is T.SKIPPED
    assert run.status is S.FAILED


# --------------------------------------------------------------------------- chaos: lost messages
def test_chaos_broker_flushed_run_still_completes(harness: EngineHarness) -> None:
    """Root task is queued in Postgres, but its Redis message is gone (FLUSHALL, broker restart)."""
    run_id = harness.start_run(
        flow(NOOP, {"key": "b", "operator": "noop", "depends_on": ["a"]}), dispatch=False
    )
    scheduler = make_scheduler(harness)
    harness.clock.advance(minutes=5)
    scheduler.tick()  # not stale yet: a slow queue isn't a lost message
    assert harness.run(run_id)[0].status is S.PENDING

    harness.clock.advance(minutes=6)
    report = scheduler.tick()
    assert report.by_job["reap_stale_tasks"] == 1
    run, tasks = harness.run(run_id)
    assert run.status is S.SUCCEEDED
    assert {t.status for t in tasks.values()} == {T.SUCCEEDED}


def test_reaped_duplicate_message_is_harmless(harness: EngineHarness) -> None:
    """The original message wasn't lost after all: both copies arrive, one execution happens."""
    run_id = harness.start_run(flow(NOOP), dispatch=False)
    recorder = RecordingDispatcher()
    harness.clock.advance(minutes=11)
    make_scheduler(harness, recorder).tick()
    _, tasks = harness.run(run_id)
    assert harness.executor.execute(tasks["a"].id) == []  # the "original" message
    assert harness.executor.execute(recorder.requests[0].task_run_id) == []  # the re-enqueued copy
    assert harness.run(run_id)[1]["a"].attempt_count == 1


# --------------------------------------------------------------------------- timeouts & retries
def test_run_deadline_fails_the_run_and_cancels_open_tasks(harness: EngineHarness) -> None:
    run_id = harness.start_run(
        flow(NOOP, {"key": "b", "operator": "noop", "depends_on": ["a"]}, timeout_seconds=60),
        dispatch=False,
    )
    scheduler = make_scheduler(harness, RecordingDispatcher())
    harness.clock.advance(seconds=59)
    scheduler.tick()
    assert harness.run(run_id)[0].status is S.PENDING
    harness.clock.advance(seconds=2)
    scheduler.tick()
    run, tasks = harness.run(run_id)
    assert run.status is S.FAILED
    assert run.error_message == "run timed out (deadline exceeded)"
    assert {t.status for t in tasks.values()} == {T.CANCELLED}
    # A message that arrives late is refused, not executed.
    assert harness.executor.execute(tasks["a"].id) == []


def test_scheduler_dispatches_due_retries(harness: EngineHarness) -> None:
    harness.llm.queue("not json", {"ok": True})
    definition = flow(
        {
            "key": "ask",
            "operator": "ai.structured",
            "retries": 1,
            "retry_backoff_seconds": 30,
            "inputs": {
                "provider": "fake",
                "model": "m",
                "prompt": "p",
                "output_schema": {"type": "object"},
            },
        }
    )
    run_id = harness.start_run(definition)
    assert harness.run(run_id)[1]["ask"].status is T.UP_FOR_RETRY
    harness.clock.advance(seconds=31)
    make_scheduler(harness).tick()
    assert harness.run(run_id)[0].status is S.SUCCEEDED


def test_one_failing_job_does_not_stop_the_tick(harness: EngineHarness) -> None:
    scheduler = make_scheduler(harness)

    def boom() -> list[Any]:
        raise RuntimeError("database hiccup")

    scheduler._job_order[0] = ("expire_runs", boom)
    report = scheduler.tick()
    assert report.errors == {"expire_runs": "RuntimeError: database hiccup"}
    assert set(report.by_job) == {
        "reap_stale_tasks",
        "dispatch_retries",
        "fire_cron_triggers",
        "admit_waiting_runs",
    }


def test_next_fire_after_is_consistent_with_stored_trigger(harness: EngineHarness) -> None:
    workflow_id, _ = harness.create_workflow(flow(NOOP))
    trigger_id = add_cron(harness, workflow_id, "0 * * * *", minutes_overdue=1)
    make_scheduler(harness).tick()
    row = trigger(harness, trigger_id)
    assert row.last_fired_at is not None
    assert row.next_fire_at == next_fire_after("0 * * * *", "UTC", row.last_fired_at)
