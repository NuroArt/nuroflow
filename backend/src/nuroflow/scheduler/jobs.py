"""Scheduler jobs. Each job is idempotent, HA-safe, and returns work to dispatch.

Every job:

* runs in short transactions and returns ``EnqueueRequest``s, which the loop dispatches
  only **after** commit (ADR-0001);
* claims rows with ``SKIP LOCKED``, so N scheduler replicas split the work and never
  block on or double-process each other (ADR-0002);
* takes locks in the global order **workflow → trigger → run → task**. Candidates
  are found with an unlocked read, then locked one by one in that order and
  re-checked, because a row may have changed between the read and the lock.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from nuroflow.core.clock import Clock
from nuroflow.db.models import TaskRun
from nuroflow.domain.enums import AttemptStatus, RunStatus, RunTriggerType, TaskStatus
from nuroflow.domain.schedule import due_slots, next_fire_after
from nuroflow.domain.state import ensure_run_transition, ensure_task_transition
from nuroflow.engine.dispatch import EnqueueRequest
from nuroflow.engine.orchestrator import Orchestrator
from nuroflow.engine.planner import RunTrigger, admit_run, plan_run
from nuroflow.engine.recovery import dispatch_due_retries
from nuroflow.engine.store import EngineStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    batch_size: int = 100
    cron_max_catchup_runs: int = 50
    queued_timeout: timedelta = timedelta(minutes=10)
    heartbeat_timeout: timedelta = timedelta(seconds=90)


class SchedulerJobs:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        clock: Clock,
        orchestrator: Orchestrator,
        queue_for: Callable[[str], str],
        config: SchedulerConfig,
    ) -> None:
        self._sessions = session_factory
        self._clock = clock
        self._orchestrator = orchestrator
        self._queue_for = queue_for
        self._config = config

    # ------------------------------------------------------------------ #
    # 1. Cron
    # ------------------------------------------------------------------ #
    def fire_cron_triggers(self) -> list[EnqueueRequest]:
        now = self._clock.now()
        with self._sessions() as session:
            candidates = EngineStore(session).due_cron_trigger_ids(now, self._config.batch_size)
        requests: list[EnqueueRequest] = []
        for workflow_id, trigger_id in candidates:
            requests += self._fire_one(workflow_id, trigger_id, now)
        return requests

    def _fire_one(
        self, workflow_id: uuid.UUID, trigger_id: uuid.UUID, now: datetime
    ) -> list[EnqueueRequest]:
        with self._sessions() as session, session.begin():
            store = EngineStore(session)
            workflow = store.lock_workflow(workflow_id, skip_locked=True)
            if workflow is None or workflow.is_paused or workflow.deleted_at is not None:
                return []
            trigger = store.lock_trigger(trigger_id)
            if (
                trigger is None
                or not trigger.is_enabled
                or trigger.next_fire_at is None
                or trigger.next_fire_at > now
                or trigger.cron_expression is None
                or workflow.current_version_id is None
            ):
                return []

            expression, tz = trigger.cron_expression, trigger.timezone
            if trigger.catchup:
                slots = list(
                    due_slots(
                        expression,
                        tz,
                        first=trigger.next_fire_at,
                        until=now,
                        limit=self._config.cron_max_catchup_runs,
                    )
                )
            else:
                # No catchup: fire once, for the most recent missed slot.
                latest = trigger.next_fire_at
                while (following := next_fire_after(expression, tz, latest)) <= now:
                    latest = following
                slots = [latest]

            spec = store.spec(workflow.current_version_id)
            active = store.count_admitted_active(workflow.id)
            requests: list[EnqueueRequest] = []
            for slot in slots:
                key = f"cron:{trigger.id}:{slot.isoformat()}"
                if store.run_exists(key):
                    continue
                admit = active < spec.max_active_runs
                run, run_requests = plan_run(
                    workflow_id=workflow.id,
                    version_id=workflow.current_version_id,
                    spec=spec,
                    trigger=RunTrigger(
                        type=RunTriggerType.CRON,
                        payload={
                            "scheduled_for": slot.isoformat(),
                            "cron": expression,
                            "timezone": tz,
                        },
                        trigger_id=trigger.id,
                        idempotency_key=key,
                        scheduled_for=slot,
                    ),
                    params={},
                    queue_for=self._queue_for,
                    now=now,
                    admit=admit,
                )
                session.add(run)
                active += 1 if admit else 0
                requests += run_requests

            trigger.last_fired_at = slots[-1]
            trigger.next_fire_at = next_fire_after(expression, tz, slots[-1])
            logger.info(
                "cron fired",
                extra={
                    "trigger_id": str(trigger.id),
                    "runs": len(slots),
                    "next_fire_at": trigger.next_fire_at.isoformat(),
                },
            )
            return requests

    # ------------------------------------------------------------------ #
    # 2. Admission (backpressure)
    # ------------------------------------------------------------------ #
    def admit_waiting_runs(self) -> list[EnqueueRequest]:
        with self._sessions() as session:
            workflow_ids = EngineStore(session).workflows_with_waiting_runs(self._config.batch_size)
        requests: list[EnqueueRequest] = []
        for workflow_id in workflow_ids:
            with self._sessions() as session, session.begin():
                store = EngineStore(session)
                workflow = store.lock_workflow(workflow_id, skip_locked=True)
                if workflow is None or workflow.is_paused or workflow.current_version_id is None:
                    continue
                now = self._clock.now()
                # Same source of truth as every launch path: the current version's spec
                # (workflows.max_active_runs is only a denormalised copy for listings).
                limit = store.spec(workflow.current_version_id).max_active_runs
                slots = limit - store.count_admitted_active(workflow.id)
                if slots <= 0:
                    continue
                for run in store.lock_waiting_runs(workflow.id, slots):
                    spec = store.spec(run.workflow_version_id)
                    requests += admit_run(run, store.task_runs(run.id), spec, now=now)
        return requests

    # ------------------------------------------------------------------ #
    # 3. Retries whose backoff elapsed
    # ------------------------------------------------------------------ #
    def dispatch_retries(self) -> list[EnqueueRequest]:
        return dispatch_due_retries(
            self._sessions, clock=self._clock, limit=self._config.batch_size
        )

    # ------------------------------------------------------------------ #
    # 4. Run timeouts
    # ------------------------------------------------------------------ #
    def expire_runs(self) -> list[EnqueueRequest]:
        now = self._clock.now()
        with self._sessions() as session, session.begin():
            store = EngineStore(session)
            for run in store.lock_timed_out_runs(now, self._config.batch_size):
                ensure_run_transition(run.status, RunStatus.FAILED)
                run.status = RunStatus.FAILED
                run.finished_at = now
                run.error_message = "run timed out (deadline exceeded)"
                cancelled = store.cancel_open_tasks(run.id, now)
                logger.warning(
                    "run timed out", extra={"run_id": str(run.id), "tasks_cancelled": cancelled}
                )
        return []  # nothing to dispatch; tasks still running finish into a terminal run

    # ------------------------------------------------------------------ #
    # 5. Reaper: lost messages and dead workers
    # ------------------------------------------------------------------ #
    def reap_stale_tasks(self) -> list[EnqueueRequest]:
        now = self._clock.now()
        with self._sessions() as session:
            candidates = EngineStore(session).stale_task_candidates(
                queued_before=now - self._config.queued_timeout,
                heartbeat_before=now - self._config.heartbeat_timeout,
                limit=self._config.batch_size,
            )
        requests: list[EnqueueRequest] = []
        for run_id, task_id in candidates:
            requests += self._reap_one(run_id, task_id, now)
        return requests

    def _reap_one(
        self, run_id: uuid.UUID, task_id: uuid.UUID, now: datetime
    ) -> list[EnqueueRequest]:
        with self._sessions() as session, session.begin():
            store = EngineStore(session)
            run = store.lock_run_nowait(run_id)  # lock order: run, then task
            task = store.task_run(task_id, lock=True) if run is not None else None
            if run is None or task is None:
                return []
            spec = store.spec(run.workflow_version_id)
            timeout = spec.task(task.task_key).timeout_seconds

            queued_too_long = (
                task.status is TaskStatus.QUEUED
                and task.queued_at is not None
                and task.queued_at < now - self._config.queued_timeout
            )
            if queued_too_long:
                if run.status.is_terminal:
                    ensure_task_transition(task.status, TaskStatus.CANCELLED)
                    task.status, task.finished_at = TaskStatus.CANCELLED, now
                    return []
                # The message was probably lost (broker restart, flush). Duplicates are
                # harmless: the executor's compare-and-set claim discards them.
                task.queued_at = now
                logger.warning(
                    "re-enqueueing stale queued task", extra={"task_run_id": str(task.id)}
                )
                return [EnqueueRequest(task.id, task.queue, timeout)]

            if (
                task.status is TaskStatus.RUNNING
                and task.heartbeat_at is not None
                and task.heartbeat_at < now - self._config.heartbeat_timeout
            ):
                return self._fail_lost_attempt(store, task, now)
            return []

    def _fail_lost_attempt(
        self, store: EngineStore, task: TaskRun, now: datetime
    ) -> list[EnqueueRequest]:
        """The worker died mid-attempt (OOM kill, lost node). Close the attempt as
        failed, then retry if allowed. The attempt still counts, so a task that
        reliably crashes its worker can't retry forever."""
        silence = int((now - (task.heartbeat_at or now)).total_seconds())
        message = f"worker lost: no heartbeat for {silence}s"
        attempt = store.attempt(task.id, task.attempt_count)
        if attempt.status is AttemptStatus.RUNNING:
            attempt.status = AttemptStatus.FAILED
            attempt.error_type = "WorkerLostError"
            attempt.error_message = message
            attempt.finished_at = now
        task.error_message = message
        logger.warning(
            "reaped task from lost worker",
            extra={"task_run_id": str(task.id), "attempt": task.attempt_count},
        )
        if task.attempt_count < task.max_attempts:
            ensure_task_transition(task.status, TaskStatus.UP_FOR_RETRY)
            task.status = TaskStatus.UP_FOR_RETRY
            task.next_attempt_at = now  # retry immediately: the failure wasn't the task's fault
            return []
        ensure_task_transition(task.status, TaskStatus.FAILED)
        task.status, task.finished_at = TaskStatus.FAILED, now
        return self._orchestrator.advance(store, task.workflow_run_id)
