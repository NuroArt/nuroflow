"""Domain enumerations shared by the ORM, the API schemas and the engine.

These are ``StrEnum`` so they serialise as plain strings in JSON, logs and SQL. The
database stores them as ``VARCHAR`` with a ``CHECK`` constraint (ADR-0006), so adding a
value is a one-line change here plus a constraint swap in a migration.
"""

from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "admin"  # manage users and every resource
    EDITOR = "editor"  # create/modify own workflows, connections and triggers
    VIEWER = "viewer"  # read-only access to own resources


class ConnectionType(StrEnum):
    TELEGRAM = "telegram"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    HTTP = "http"


class TriggerType(StrEnum):
    """Persistent triggers stored in ``workflow_triggers``."""

    CRON = "cron"
    EVENT = "event"
    WEBHOOK = "webhook"


class RunTriggerType(StrEnum):
    """What actually started a run (manual runs have no stored trigger row)."""

    MANUAL = "manual"
    CRON = "cron"
    EVENT = "event"
    WEBHOOK = "webhook"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_RUN_STATUSES


_TERMINAL_RUN_STATUSES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})


class TaskStatus(StrEnum):
    PENDING = "pending"  # waiting on upstream tasks
    QUEUED = "queued"  # message published, waiting for a worker
    RUNNING = "running"  # claimed by a worker
    UP_FOR_RETRY = "up_for_retry"  # failed attempt, waiting for backoff to elapse
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"  # not executed because of upstream outcome / trigger rule
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_TASK_STATUSES


_TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.SKIPPED, TaskStatus.CANCELLED}
)


class AttemptStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ChannelType(StrEnum):
    TELEGRAM = "telegram"
    WEBHOOK = "webhook"


class RunEvent(StrEnum):
    """Lifecycle events that notification subscriptions can listen to."""

    RUN_STARTED = "run.started"
    RUN_SUCCEEDED = "run.succeeded"
    RUN_FAILED = "run.failed"
    TASK_RETRYING = "task.retrying"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
