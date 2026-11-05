"""State machines and trigger rules: the engine's rules of motion, as data.

Every status change in the engine goes through :func:`ensure_task_transition` /
:func:`ensure_run_transition`. An illegal transition is a **bug** (e.g. a stale
worker overwriting a terminal task), so it raises loudly instead of silently
corrupting history.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from nuroflow.domain.enums import RunStatus, TaskStatus

T = TaskStatus
R = RunStatus

TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    T.PENDING: frozenset({T.QUEUED, T.SKIPPED, T.CANCELLED}),
    T.QUEUED: frozenset({T.RUNNING, T.QUEUED, T.CANCELLED}),  # QUEUED→QUEUED: re-enqueue by reaper
    T.RUNNING: frozenset({T.SUCCEEDED, T.FAILED, T.UP_FOR_RETRY, T.CANCELLED}),
    T.UP_FOR_RETRY: frozenset({T.QUEUED, T.CANCELLED}),
    T.SUCCEEDED: frozenset(),
    T.FAILED: frozenset(),
    T.SKIPPED: frozenset(),
    T.CANCELLED: frozenset(),
}

RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    R.PENDING: frozenset({R.RUNNING, R.SUCCEEDED, R.FAILED, R.CANCELLED}),
    R.RUNNING: frozenset({R.SUCCEEDED, R.FAILED, R.CANCELLED}),
    R.SUCCEEDED: frozenset(),
    R.FAILED: frozenset(),
    R.CANCELLED: frozenset(),
}


class InvalidStateTransition(RuntimeError):
    def __init__(self, kind: str, current: StrEnum, target: StrEnum) -> None:
        super().__init__(f"illegal {kind} transition: {current} → {target}")


def ensure_task_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target not in TASK_TRANSITIONS[current]:
        raise InvalidStateTransition("task", current, target)


def ensure_run_transition(current: RunStatus, target: RunStatus) -> None:
    if target not in RUN_TRANSITIONS[current]:
        raise InvalidStateTransition("run", current, target)


# --------------------------------------------------------------------------- #
# Trigger rules
# --------------------------------------------------------------------------- #
class TriggerRule(StrEnum):
    ALL_SUCCESS = "all_success"  # default: every upstream succeeded
    NONE_FAILED = "none_failed"  # upstream succeeded or was skipped
    ONE_FAILED = "one_failed"  # error handlers: run only if something upstream failed
    ALL_DONE = "all_done"  # always run once upstream finished (cleanup, notifications)


class Readiness(StrEnum):
    WAIT = "wait"
    READY = "ready"
    SKIP = "skip"


def evaluate_trigger_rule(rule: TriggerRule, upstream: Iterable[TaskStatus]) -> Readiness:
    """Decide what a PENDING task should do given its direct upstream statuses.

    Every rule waits until all upstream tasks are terminal. That keeps semantics simple
    and predictable (Airflow's "eager" ONE_FAILED variant is deliberately not offered).
    """
    statuses = list(upstream)
    if not statuses:
        return Readiness.READY
    if not all(s.is_terminal for s in statuses):
        return Readiness.WAIT

    failed = any(s in (T.FAILED, T.CANCELLED) for s in statuses)
    match rule:
        case TriggerRule.ALL_SUCCESS:
            ok = all(s is T.SUCCEEDED for s in statuses)
        case TriggerRule.NONE_FAILED:
            ok = not failed
        case TriggerRule.ONE_FAILED:
            ok = failed
        case TriggerRule.ALL_DONE:
            ok = True
    return Readiness.READY if ok else Readiness.SKIP


def final_run_status(task_statuses: Iterable[TaskStatus]) -> RunStatus | None:
    """The run's outcome once every task is terminal, else ``None``.

    A run fails if any task failed, even if an ``one_failed`` handler ran
    successfully afterwards. Handling an error doesn't make it not-an-error, and the
    run history should say so.
    """
    statuses = list(task_statuses)
    if not all(s.is_terminal for s in statuses):
        return None
    if any(s is T.FAILED for s in statuses):
        return R.FAILED
    if statuses and all(s is T.CANCELLED for s in statuses):
        return R.CANCELLED
    return R.SUCCEEDED
