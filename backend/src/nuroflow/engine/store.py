"""Synchronous persistence for the engine (the worker-side repository, ADR-0005).

Lock ordering rule, which keeps the engine deadlock-free: whenever both are locked in
one transaction, lock the **workflow_run row first, then task_run rows**. Every method
that takes locks documents which.
"""

from __future__ import annotations

import json
import uuid
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from datetime import datetime
from threading import Lock
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from nuroflow.db.models import TaskAttempt, TaskLog, TaskRun, Workflow, WorkflowRun, WorkflowVersion
from nuroflow.domain.enums import TaskStatus
from nuroflow.domain.workflow_spec import WorkflowSpec
from nuroflow.operators.base import LogLine


class _SpecCache:
    """Versions are immutable, so a parsed spec can be cached forever (bounded LRU)."""

    def __init__(self, maxsize: int = 512) -> None:
        self._data: OrderedDict[uuid.UUID, WorkflowSpec] = OrderedDict()
        self._maxsize = maxsize
        self._lock = Lock()

    def get(self, key: uuid.UUID) -> WorkflowSpec | None:
        with self._lock:
            spec = self._data.get(key)
            if spec is not None:
                self._data.move_to_end(key)
            return spec

    def put(self, key: uuid.UUID, spec: WorkflowSpec) -> None:
        with self._lock:
            self._data[key] = spec
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)


SPEC_CACHE = _SpecCache()


class EngineStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---- reads -------------------------------------------------------------- #
    def task_run(self, task_run_id: uuid.UUID, *, lock: bool = False) -> TaskRun | None:
        stmt = sa.select(TaskRun).where(TaskRun.id == task_run_id)
        if lock:
            stmt = stmt.with_for_update()
        return self.session.scalars(stmt).one_or_none()

    def lock_run(self, run_id: uuid.UUID) -> WorkflowRun:
        """Row-lock the run. Serialises DAG advancement for this run (lock #1)."""
        return self.session.scalars(
            sa.select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update()
        ).one()

    def task_runs(self, run_id: uuid.UUID) -> Sequence[TaskRun]:
        return self.session.scalars(
            sa.select(TaskRun).where(TaskRun.workflow_run_id == run_id).order_by(TaskRun.id)
        ).all()

    def spec(self, version_id: uuid.UUID) -> WorkflowSpec:
        if (cached := SPEC_CACHE.get(version_id)) is not None:
            return cached
        definition = self.session.scalars(
            sa.select(WorkflowVersion.definition).where(WorkflowVersion.id == version_id)
        ).one()
        spec = WorkflowSpec.model_validate(definition)
        SPEC_CACHE.put(version_id, spec)
        return spec

    def version_id_for_run(self, run_id: uuid.UUID) -> uuid.UUID:
        return self.session.scalars(
            sa.select(WorkflowRun.workflow_version_id).where(WorkflowRun.id == run_id)
        ).one()

    def owner_id(self, workflow_id: uuid.UUID) -> uuid.UUID:
        return self.session.scalars(
            sa.select(Workflow.owner_id).where(Workflow.id == workflow_id)
        ).one()

    def outputs(self, run_id: uuid.UUID, keys: Iterable[str]) -> dict[str, TaskRun]:
        keys = list(keys)
        if not keys:
            return {}
        rows = self.session.scalars(
            sa.select(TaskRun).where(TaskRun.workflow_run_id == run_id, TaskRun.task_key.in_(keys))
        ).all()
        return {r.task_key: r for r in rows}

    def attempt(self, task_run_id: uuid.UUID, number: int) -> TaskAttempt:
        return self.session.scalars(
            sa.select(TaskAttempt).where(
                TaskAttempt.task_run_id == task_run_id, TaskAttempt.attempt_number == number
            )
        ).one()

    # ---- writes ------------------------------------------------------------- #
    def claim(self, task_run_id: uuid.UUID, *, worker_id: str, now: datetime) -> int | None:
        """Compare-and-set queued → running. Returns the new attempt number, or None if
        another delivery already claimed it (or it was cancelled). Caller holds the run lock."""
        return self.session.execute(
            sa.update(TaskRun)
            .where(TaskRun.id == task_run_id, TaskRun.status == TaskStatus.QUEUED)
            .values(
                status=TaskStatus.RUNNING,
                attempt_count=TaskRun.attempt_count + 1,
                started_at=now,
                heartbeat_at=now,
                worker_id=worker_id,
                next_attempt_at=None,
            )
            .returning(TaskRun.attempt_count)
        ).scalar_one_or_none()

    def heartbeat(self, task_run_id: uuid.UUID, attempt: int, now: datetime) -> bool:
        result = self.session.execute(
            sa.update(TaskRun)
            .where(
                TaskRun.id == task_run_id,
                TaskRun.status == TaskStatus.RUNNING,
                TaskRun.attempt_count == attempt,
            )
            .values(heartbeat_at=now)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    def add_logs(self, task_run_id: uuid.UUID, attempt: int, lines: Sequence[LogLine]) -> None:
        if not lines:
            return
        self.session.execute(
            sa.insert(TaskLog),
            [
                {
                    "task_run_id": task_run_id,
                    "attempt_number": attempt,
                    "level": line.level,
                    "message": line.message,
                    "context": _json_safe(line.context),
                    "created_at": line.at,
                }
                for line in lines
            ],
        )

    def due_retries(self, now: datetime, limit: int) -> Sequence[TaskRun]:
        """Up-for-retry tasks whose backoff has elapsed; SKIP LOCKED makes this HA-safe."""
        return self.session.scalars(
            sa.select(TaskRun)
            .where(TaskRun.status == TaskStatus.UP_FOR_RETRY, TaskRun.next_attempt_at <= now)
            .order_by(TaskRun.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()


def _json_safe(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(json.dumps(value, default=str))
    return result
