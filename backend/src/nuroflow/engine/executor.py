"""Executor: runs ONE task attempt, crash-safely. Three short transactions, one long call.

::

    T1  claim      lock run → CAS queued→running → insert attempt → commit
        (no transaction held)
        render inputs → validate → operator.execute()  ← may take minutes (LLM, HTTP)
    T2  record     lock run → lock task → verify we still own it → write outcome,
                   logs, metrics → orchestrator.advance() → commit
        return newly-ready tasks for dispatch (caller enqueues AFTER commit)

Why not one transaction? Holding a DB connection and row locks for the duration of
an LLM call would exhaust the connection pool and block every other task in the run.

Ownership check in T2: if this worker hung long enough for the reaper (Phase 4) to
hand the task to someone else, ``attempt_count`` has moved on, and our late result is
discarded instead of overwriting the newer attempt.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.orm import Session, sessionmaker

from nuroflow.core.clock import Clock
from nuroflow.core.logging import bind_log_context
from nuroflow.db.models import TaskAttempt
from nuroflow.domain.enums import AttemptStatus, RunEvent, RunStatus, TaskStatus
from nuroflow.domain.state import ensure_run_transition, ensure_task_transition
from nuroflow.engine.dispatch import EnqueueRequest
from nuroflow.engine.heartbeat import Heartbeat
from nuroflow.engine.orchestrator import Orchestrator
from nuroflow.engine.retry import RetryPolicy
from nuroflow.engine.store import EngineStore
from nuroflow.engine.templating import TemplateRenderer
from nuroflow.notifications.outbox import emit
from nuroflow.observability import metrics
from nuroflow.operators.base import (
    NonRetryableError,
    OperatorContext,
    TaskError,
    TaskTimeoutError,
)
from nuroflow.operators.registry import OperatorRegistry, UnknownOperatorError

logger = logging.getLogger(__name__)

MAX_ERROR_CHARS = 4000
MAX_TRACEBACK_CHARS = 20_000


@dataclass(frozen=True, slots=True)
class _Claim:
    attempt: int
    ctx: OperatorContext
    operator: str
    raw_inputs: dict[str, Any]
    template_context: dict[str, Any]
    max_attempts: int
    retry: RetryPolicy


@dataclass(slots=True)
class _Outcome:
    output: dict[str, Any] | None = None
    rendered_inputs: Any = None
    error: BaseException | None = None
    retryable: bool = False
    duration_ms: int = 0
    logs_flushed: int = 0  # already persisted live by the heartbeat thread


class TaskExecutor:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        registry: OperatorRegistry,
        orchestrator: Orchestrator,
        clock: Clock,
        renderer: TemplateRenderer | None = None,
        output_max_bytes: int = 256 * 1024,
        heartbeat_interval_seconds: float = 15.0,
        worker_id: str | None = None,
    ) -> None:
        self._sessions = session_factory
        self._registry = registry
        self._orchestrator = orchestrator
        self._clock = clock
        self._renderer = renderer or TemplateRenderer()
        self._output_max_bytes = output_max_bytes
        self._heartbeat_interval = heartbeat_interval_seconds
        self._worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"

    # ------------------------------------------------------------------ #
    def execute(self, task_run_id: uuid.UUID) -> list[EnqueueRequest]:
        with bind_log_context(task_run_id=str(task_run_id)):
            claim = self._claim(task_run_id)
            if claim is None:
                return []
            with bind_log_context(
                run_id=str(claim.ctx.run_id),
                task_key=claim.ctx.task_key,
                operator=claim.operator,
                attempt=claim.attempt,
            ):
                outcome = self._run_operator(claim)
                return self._record(task_run_id, claim, outcome)

    # ---- T1 ----------------------------------------------------------- #
    def _claim(self, task_run_id: uuid.UUID) -> _Claim | None:
        with self._sessions() as session, session.begin():
            store = EngineStore(session)
            task = store.task_run(task_run_id)
            if task is None:
                logger.warning("task run not found", extra={"task_run_id": str(task_run_id)})
                return None
            run = store.lock_run(task.workflow_run_id)
            now = self._clock.now()

            if run.status.is_terminal:  # cancelled (or timed out) while the message was queued
                if task.status in (TaskStatus.QUEUED, TaskStatus.PENDING):
                    ensure_task_transition(task.status, TaskStatus.CANCELLED)
                    task.status, task.finished_at = TaskStatus.CANCELLED, now
                return None

            attempt = store.claim(task_run_id, worker_id=self._worker_id, now=now)
            if attempt is None:
                logger.info(
                    "duplicate delivery ignored",
                    extra={"task_run_id": str(task_run_id), "status": task.status.value},
                )
                return None
            session.refresh(task)

            if run.status is RunStatus.PENDING:
                ensure_run_transition(run.status, RunStatus.RUNNING)
                run.status, run.started_at = RunStatus.RUNNING, now
                emit(session, run, RunEvent.RUN_STARTED, now=now)

            session.add(
                TaskAttempt(
                    task_run_id=task.id,
                    attempt_number=attempt,
                    worker_id=self._worker_id,
                    started_at=now,
                    status=AttemptStatus.RUNNING,
                )
            )

            spec = store.spec(run.workflow_version_id)
            resolved = spec.task(task.task_key)
            upstream = store.outputs(run.id, spec.dag.ancestors(task.task_key))
            ctx = OperatorContext(
                workflow_id=run.workflow_id,
                run_id=run.id,
                task_run_id=task.id,
                task_key=task.task_key,
                owner_id=store.owner_id(run.workflow_id),
                attempt=attempt,
                timeout_seconds=resolved.timeout_seconds,
            )
            template_context = {
                "params": run.params,
                "trigger": {"type": run.trigger_type.value, "payload": run.trigger_payload},
                "tasks": {
                    key: {"output": t.output, "status": t.status.value}
                    for key, t in upstream.items()
                },
                "run": {
                    "id": str(run.id),
                    "workflow_id": str(run.workflow_id),
                    "scheduled_for": run.scheduled_for.isoformat() if run.scheduled_for else None,
                    "attempt": attempt,
                },
            }
            return _Claim(
                attempt=attempt,
                ctx=ctx,
                operator=task.operator,
                raw_inputs=resolved.inputs,
                template_context=template_context,
                max_attempts=task.max_attempts,
                retry=RetryPolicy(
                    base_seconds=resolved.retry_backoff_seconds,
                    max_seconds=resolved.retry_backoff_max_seconds,
                ),
            )

    # ---- outside any transaction --------------------------------------- #
    def _run_operator(self, claim: _Claim) -> _Outcome:
        outcome = _Outcome()
        started = time.perf_counter()
        try:
            outcome.rendered_inputs = self._renderer.render(
                claim.raw_inputs, claim.template_context
            )
            operator = self._registry.get(claim.operator)
            heartbeat = Heartbeat(
                self._sessions,
                claim.ctx.task_run_id,
                claim.attempt,
                clock=self._clock,
                interval_seconds=self._heartbeat_interval,
                logs=claim.ctx.logs,
            )
            try:
                with heartbeat:
                    output = operator.run(outcome.rendered_inputs, claim.ctx)
            finally:
                outcome.logs_flushed = heartbeat.flushed
            outcome.output = self._check_output(output)
        except UnknownOperatorError as exc:
            outcome.error = NonRetryableError(f"operator '{exc.args[0]}' is not installed")
        except SoftTimeLimitExceeded:
            outcome.error = TaskTimeoutError(f"timed out after {claim.ctx.timeout_seconds}s")
            outcome.retryable = True
        except TaskError as exc:
            outcome.error, outcome.retryable = exc, exc.retryable
        except Exception as exc:  # unknown failure → recorded with traceback, retryable
            outcome.error, outcome.retryable = exc, True
        outcome.duration_ms = int((time.perf_counter() - started) * 1000)
        return outcome

    def _check_output(self, output: Any) -> dict[str, Any]:
        if not isinstance(output, dict):
            raise NonRetryableError(
                f"operator returned {type(output).__name__}, expected an object"
            )
        try:
            encoded = json.dumps(output, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise NonRetryableError(f"operator output is not JSON-serialisable: {exc}") from exc
        if len(encoded.encode()) > self._output_max_bytes:
            raise NonRetryableError(
                f"output is {len(encoded.encode())} bytes; limit is {self._output_max_bytes}"
            )
        result: dict[str, Any] = json.loads(encoded)
        return result

    # ---- T2 ----------------------------------------------------------- #
    def _record(
        self, task_run_id: uuid.UUID, claim: _Claim, outcome: _Outcome
    ) -> list[EnqueueRequest]:
        with self._sessions() as session, session.begin():
            store = EngineStore(session)
            run = store.lock_run(claim.ctx.run_id)
            task = store.task_run(task_run_id, lock=True)
            if (
                task is None
                or task.status is not TaskStatus.RUNNING
                or task.attempt_count != claim.attempt
            ):
                logger.warning(
                    "stale result discarded: task was reassigned",
                    extra={"task_run_id": str(task_run_id), "attempt": claim.attempt},
                )
                return []

            now = self._clock.now()
            attempt = store.attempt(task.id, claim.attempt)
            attempt.finished_at = now
            attempt.duration_ms = outcome.duration_ms
            attempt.metrics = claim.ctx.metrics
            store.add_logs(task.id, claim.attempt, claim.ctx.logs[outcome.logs_flushed :])
            if outcome.rendered_inputs is not None:
                task.input = _json_or_none(outcome.rendered_inputs)

            if outcome.error is None:
                ensure_task_transition(task.status, TaskStatus.SUCCEEDED)
                task.status, task.output, task.error_message = (
                    TaskStatus.SUCCEEDED,
                    outcome.output,
                    None,
                )
                task.finished_at = now
                attempt.status = AttemptStatus.SUCCEEDED
            else:
                error = outcome.error
                attempt.status = AttemptStatus.FAILED
                attempt.error_type = type(error).__name__
                attempt.error_message = str(error)[:MAX_ERROR_CHARS] or type(error).__name__
                attempt.traceback = "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                )[-MAX_TRACEBACK_CHARS:]
                task.error_message = attempt.error_message
                if outcome.retryable and claim.attempt < claim.max_attempts:
                    ensure_task_transition(task.status, TaskStatus.UP_FOR_RETRY)
                    task.status = TaskStatus.UP_FOR_RETRY
                    task.next_attempt_at = now + claim.retry.delay(claim.attempt)
                    emit(
                        session,
                        run,
                        RunEvent.TASK_RETRYING,
                        now=now,
                        task_key=task.task_key,
                        attempt=claim.attempt,
                        error=attempt.error_message,
                    )
                else:
                    ensure_task_transition(task.status, TaskStatus.FAILED)
                    task.status, task.finished_at = TaskStatus.FAILED, now
                logger.warning(
                    "task attempt failed",
                    extra={
                        "task_run_id": str(task.id),
                        "attempt": claim.attempt,
                        "error_type": attempt.error_type,
                        "will_retry": task.status is TaskStatus.UP_FOR_RETRY,
                    },
                )

            attempt_outcome = (
                "succeeded"
                if task.status is TaskStatus.SUCCEEDED
                else "retrying"
                if task.status is TaskStatus.UP_FOR_RETRY
                else "failed"
            )
            requests = (
                self._orchestrator.advance(store, claim.ctx.run_id)
                if task.status.is_terminal
                else []
            )
            run_finished = run.status if run.status.is_terminal else None
        # Committed: only now is it true that this attempt happened.
        record_attempt_metrics(claim.operator, attempt_outcome, outcome.duration_ms, claim.ctx)
        if run_finished is not None:
            metrics.RUNS_FINISHED.labels(status=run_finished.value).inc()
        return requests


def _json_or_none(value: Any) -> dict[str, Any] | None:
    try:
        encoded = json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return None
    return encoded if isinstance(encoded, dict) else {"value": encoded}


def record_attempt_metrics(
    operator: str, outcome: str, duration_ms: int, ctx: OperatorContext
) -> None:
    metrics.TASK_ATTEMPTS.labels(operator=operator, outcome=outcome).inc()
    metrics.TASK_DURATION.labels(operator=operator).observe(duration_ms / 1000)
    llm = ctx.metrics.get("llm")
    if isinstance(llm, dict):
        provider, model = str(llm.get("provider")), str(llm.get("model"))
        for kind in ("input", "output"):
            tokens = llm.get(f"{kind}_tokens") or 0
            metrics.LLM_TOKENS.labels(provider=provider, model=model, kind=kind).inc(tokens)
        if llm.get("cost_usd"):
            metrics.LLM_COST.labels(provider=provider, model=model).inc(llm["cost_usd"])
