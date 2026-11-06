"""Background heartbeat while an operator runs.

Every ``interval`` seconds a daemon thread bumps ``task_runs.heartbeat_at`` in its own
short transaction. The reaper (Phase 4) treats a RUNNING task with a stale heartbeat
as orphaned (worker OOM-killed, node lost) and reschedules it.

The update is guarded by ``attempt_count``, so a heartbeat from a zombie attempt can
never keep a newer attempt "alive".

**Live logs (Phase 6).** The same thread flushes the operator's new log lines every
``log_flush_seconds`` (default 1s), so the dashboard's SSE tail shows them while the
task is still running instead of only after it finishes. ``flushed`` records how many
lines are already persisted; the executor writes only the remainder at completion.
"""

from __future__ import annotations

import logging
import threading
import uuid
from types import TracebackType

from sqlalchemy.orm import Session, sessionmaker

from nuroflow.core.clock import Clock
from nuroflow.operators.base import LogLine

logger = logging.getLogger(__name__)


class Heartbeat:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        task_run_id: uuid.UUID,
        attempt: int,
        *,
        clock: Clock,
        interval_seconds: float,
        logs: list[LogLine] | None = None,
        log_flush_seconds: float = 1.0,
    ) -> None:
        self._sessions = session_factory
        self._task_run_id = task_run_id
        self._attempt = attempt
        self._clock = clock
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._logs = logs if logs is not None else []
        self._tick = min(interval_seconds, log_flush_seconds) if interval_seconds > 0 else 0
        self.beats = 0
        self.flushed = 0  # lines of ``logs`` already persisted

    def __enter__(self) -> Heartbeat:
        if self._interval > 0:
            self._thread = threading.Thread(
                target=self._loop, name=f"heartbeat-{self._task_run_id}", daemon=True
            )
            self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                # A flush may still be in flight: count everything as flushed rather
                # than risk writing the same lines twice. Losing a tail is the lesser evil.
                logger.warning("heartbeat thread did not stop in time")
                self.flushed = len(self._logs)

    def _loop(self) -> None:
        from nuroflow.engine.store import EngineStore

        elapsed = 0.0
        while not self._stop.wait(self._tick):
            elapsed += self._tick
            beat = elapsed >= self._interval
            # Snapshot the end index first: the operator thread may append meanwhile.
            # list.append is atomic in CPython, and we only ever read a prefix.
            end = len(self._logs)
            try:
                with self._sessions() as session, session.begin():
                    store = EngineStore(session)
                    if end > self.flushed:
                        store.add_logs(
                            self._task_run_id, self._attempt, self._logs[self.flushed : end]
                        )
                    alive = (
                        store.heartbeat(self._task_run_id, self._attempt, self._clock.now())
                        if beat
                        else True
                    )
                self.flushed = end
                if beat:
                    elapsed = 0.0
                    self.beats += 1
                if not alive:
                    logger.warning(
                        "heartbeat rejected: attempt no longer owns the task",
                        extra={"task_run_id": str(self._task_run_id), "attempt": self._attempt},
                    )
                    return
            except Exception:  # a failed beat must never kill the task itself
                logger.exception("heartbeat failed", extra={"task_run_id": str(self._task_run_id)})
