"""Background heartbeat while an operator runs.

Every ``interval`` seconds a daemon thread bumps ``task_runs.heartbeat_at`` in its own
short transaction. The reaper (Phase 4) treats a RUNNING task with a stale heartbeat
as orphaned (worker OOM-killed, node lost) and reschedules it.

The update is guarded by ``attempt_count``, so a heartbeat from a zombie attempt can
never keep a newer attempt "alive".
"""

from __future__ import annotations

import logging
import threading
import uuid
from types import TracebackType

from sqlalchemy.orm import Session, sessionmaker

from nuroflow.core.clock import Clock

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
    ) -> None:
        self._sessions = session_factory
        self._task_run_id = task_run_id
        self._attempt = attempt
        self._clock = clock
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.beats = 0

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

    def _loop(self) -> None:
        from nuroflow.engine.store import EngineStore

        while not self._stop.wait(self._interval):
            try:
                with self._sessions() as session, session.begin():
                    alive = EngineStore(session).heartbeat(
                        self._task_run_id, self._attempt, self._clock.now()
                    )
                self.beats += 1
                if not alive:
                    logger.warning(
                        "heartbeat rejected: attempt no longer owns the task",
                        extra={"task_run_id": str(self._task_run_id), "attempt": self._attempt},
                    )
                    return
            except Exception:  # a failed beat must never kill the task itself
                logger.exception("heartbeat failed", extra={"task_run_id": str(self._task_run_id)})
