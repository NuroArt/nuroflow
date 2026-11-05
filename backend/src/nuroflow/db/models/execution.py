"""Execution state: workflow runs, task runs, attempts and logs.

These are the hottest tables in the system. Every index here maps to a specific
query in the scheduler, engine or dashboard, and DATABASE.md lists which.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nuroflow.db.base import EMPTY_JSON_OBJECT, Base, str_enum
from nuroflow.db.mixins import CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from nuroflow.domain.enums import AttemptStatus, LogLevel, RunStatus, RunTriggerType, TaskStatus


class WorkflowRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        # Run history per workflow, newest first (dashboard, API list).
        sa.Index(
            "ix_workflow_runs_workflow_id_created_at", "workflow_id", sa.text("created_at DESC")
        ),
        # max_active_runs check + "what's running now" (small, partial).
        sa.Index(
            "ix_workflow_runs_active",
            "workflow_id",
            "status",
            postgresql_where=sa.text("status IN ('pending', 'running')"),
        ),
        # Scheduler timeout sweep.
        sa.Index(
            "ix_workflow_runs_deadline_running",
            "deadline_at",
            postgresql_where=sa.text("status = 'running' AND deadline_at IS NOT NULL"),
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name="finished_after_started",
        ),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workflows.id", ondelete="CASCADE")
    )
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workflow_versions.id", ondelete="RESTRICT"), index=True
    )
    trigger_type: Mapped[RunTriggerType] = mapped_column(
        str_enum(RunTriggerType, name="trigger_type")
    )
    trigger_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("workflow_triggers.id", ondelete="SET NULL"), index=True
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("events.id", ondelete="SET NULL"), index=True
    )
    triggered_by_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[RunStatus] = mapped_column(
        str_enum(RunStatus, name="status"), server_default=RunStatus.PENDING.value
    )
    params: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT)
    trigger_payload: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT)
    # Exactly-once run creation: 'cron:{trigger}:{slot}', 'event:{event}:{trigger}', or a
    # client-supplied Idempotency-Key for manual runs.
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(255), unique=True)
    scheduled_for: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    deadline_at: Mapped[datetime | None]
    error_message: Mapped[str | None] = mapped_column(sa.Text)

    task_runs: Mapped[list[TaskRun]] = relationship(
        back_populates="workflow_run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<WorkflowRun id={self.id} status={self.status}>"


class TaskRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One task inside one run. Retries add :class:`TaskAttempt` rows, not TaskRuns."""

    __tablename__ = "task_runs"
    __table_args__ = (
        sa.UniqueConstraint("workflow_run_id", "task_key"),
        # Scheduler: due retries.
        sa.Index(
            "ix_task_runs_retry_due",
            "next_attempt_at",
            postgresql_where=sa.text("status = 'up_for_retry'"),
        ),
        # Reaper: in-flight tasks whose heartbeat went stale.
        sa.Index(
            "ix_task_runs_in_flight_heartbeat",
            "heartbeat_at",
            postgresql_where=sa.text("status IN ('queued', 'running')"),
        ),
        sa.CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        sa.CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        sa.CheckConstraint("attempt_count <= max_attempts", name="attempts_within_limit"),
    )

    workflow_run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workflow_runs.id", ondelete="CASCADE")
    )
    task_key: Mapped[str] = mapped_column(sa.String(100))
    operator: Mapped[str] = mapped_column(sa.String(100))
    queue: Mapped[str] = mapped_column(sa.String(50), server_default="default")
    status: Mapped[TaskStatus] = mapped_column(
        str_enum(TaskStatus, name="status"), server_default=TaskStatus.PENDING.value
    )
    attempt_count: Mapped[int] = mapped_column(server_default=sa.text("0"))
    max_attempts: Mapped[int] = mapped_column(server_default=sa.text("1"))
    next_attempt_at: Mapped[datetime | None]
    input: Mapped[dict[str, Any] | None]  # rendered inputs of the latest attempt
    output: Mapped[dict[str, Any] | None]
    error_message: Mapped[str | None] = mapped_column(sa.Text)
    queued_at: Mapped[datetime | None]
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    heartbeat_at: Mapped[datetime | None]
    worker_id: Mapped[str | None] = mapped_column(sa.String(200))

    workflow_run: Mapped[WorkflowRun] = relationship(back_populates="task_runs", lazy="raise")
    attempts: Mapped[list[TaskAttempt]] = relationship(
        back_populates="task_run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TaskAttempt.attempt_number",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<TaskRun id={self.id} key={self.task_key!r} status={self.status}>"


class TaskAttempt(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One try of a task. It's the error-tracking record (type, message, traceback)."""

    __tablename__ = "task_attempts"
    __table_args__ = (
        sa.UniqueConstraint("task_run_id", "attempt_number"),
        sa.CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        sa.CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="duration_non_negative"),
    )

    task_run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("task_runs.id", ondelete="CASCADE")
    )
    attempt_number: Mapped[int]
    status: Mapped[AttemptStatus] = mapped_column(
        str_enum(AttemptStatus, name="status"), server_default=AttemptStatus.RUNNING.value
    )
    worker_id: Mapped[str | None] = mapped_column(sa.String(200))
    started_at: Mapped[datetime] = mapped_column(server_default=sa.func.now())
    finished_at: Mapped[datetime | None]
    duration_ms: Mapped[int | None]
    error_type: Mapped[str | None] = mapped_column(sa.String(200))
    error_message: Mapped[str | None] = mapped_column(sa.Text)
    traceback: Mapped[str | None] = mapped_column(sa.Text)
    # Operator-reported metrics: {"llm": {"provider": "anthropic", "input_tokens": 512, ...}}
    metrics: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT)

    task_run: Mapped[TaskRun] = relationship(back_populates="attempts", lazy="raise")


class TaskLog(CreatedAtMixin, Base):
    """Append-only structured log line emitted by an operator.

    BIGINT identity key: the smallest index and natural order for tailing. This is the
    first table to range-partition by ``created_at`` once volume requires it.
    """

    __tablename__ = "task_logs"
    __table_args__ = (sa.Index("ix_task_logs_task_run_id_id", "task_run_id", "id"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(always=True), primary_key=True)
    task_run_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("task_runs.id", ondelete="CASCADE")
    )
    attempt_number: Mapped[int]
    level: Mapped[LogLevel] = mapped_column(
        str_enum(LogLevel, name="level", length=16), server_default=LogLevel.INFO.value
    )
    message: Mapped[str] = mapped_column(sa.Text)
    context: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT)
