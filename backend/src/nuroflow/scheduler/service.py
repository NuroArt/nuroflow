"""The scheduler loop: runs every job each tick, then dispatches what they produced.

Deliberately a **plain synchronous loop**, not asyncio. Each job is a few short SQL
transactions executed in sequence, with no concurrent I/O to overlap, so an event loop
would add complexity and no throughput. Scale-out is horizontal instead: run more
replicas and ``SKIP LOCKED`` splits the work between them.

Job order matters within a tick:

1. ``expire_runs``: fail runs past their deadline *before* doing anything for them.
2. ``reap_stale_tasks``: turn dead-worker tasks into retries…
3. ``dispatch_retries``: …which are then re-dispatched in the same tick.
4. ``fire_cron_triggers``: create scheduled runs.
5. ``admit_waiting_runs``: give free slots to waiting runs (including those cron just created).
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from types import FrameType

from sqlalchemy.orm import Session, sessionmaker

from nuroflow.core.clock import Clock
from nuroflow.core.config import Settings
from nuroflow.engine.dispatch import EnqueueRequest, TaskDispatcher
from nuroflow.engine.orchestrator import Orchestrator
from nuroflow.scheduler.jobs import SchedulerConfig, SchedulerJobs

logger = logging.getLogger(__name__)

MAX_TASK_TIMEOUT = timedelta(hours=1)


@dataclass
class TickReport:
    dispatched: int = 0
    by_job: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    duration_ms: float = 0.0


class Scheduler:
    def __init__(
        self,
        *,
        jobs: SchedulerJobs,
        dispatcher: TaskDispatcher,
        tick_seconds: float,
    ) -> None:
        self._jobs = jobs
        self._dispatcher = dispatcher
        self._tick_seconds = tick_seconds
        self._stop = threading.Event()
        self._job_order: list[tuple[str, Callable[[], list[EnqueueRequest]]]] = [
            ("expire_runs", jobs.expire_runs),
            ("reap_stale_tasks", jobs.reap_stale_tasks),
            ("dispatch_retries", jobs.dispatch_retries),
            ("fire_cron_triggers", jobs.fire_cron_triggers),
            ("admit_waiting_runs", jobs.admit_waiting_runs),
        ]

    def tick(self) -> TickReport:
        """Run every job once. One failing job never stops the others."""
        report = TickReport()
        started = time.perf_counter()
        for name, job in self._job_order:
            try:
                requests = job()
            except Exception as exc:
                logger.exception("scheduler job failed", extra={"job": name})
                report.errors[name] = f"{type(exc).__name__}: {exc}"
                continue
            if requests:
                self._dispatcher.enqueue(requests)  # after the job's commit
            report.by_job[name] = len(requests)
            report.dispatched += len(requests)
        report.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        return report

    def run_forever(self) -> None:
        logger.info("scheduler started", extra={"tick_seconds": self._tick_seconds})
        while not self._stop.is_set():
            report = self.tick()
            if report.dispatched or report.errors:
                logger.info(
                    "scheduler tick",
                    extra={
                        "dispatched": report.by_job,
                        "errors": report.errors,
                        "duration_ms": report.duration_ms,
                    },
                )
            # Sleep the remainder of the tick, waking instantly on shutdown.
            self._stop.wait(max(0.0, self._tick_seconds - report.duration_ms / 1000))
        logger.info("scheduler stopped")

    def stop(self, *_: object) -> None:
        self._stop.set()

    def install_signal_handlers(self) -> None:
        def _handler(signum: int, frame: FrameType | None) -> None:
            logger.info("shutdown signal received", extra={"signal": signum})
            self.stop()

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)


def scheduler_config(settings: Settings) -> SchedulerConfig:
    heartbeat_timeout = (
        timedelta(seconds=settings.reaper_heartbeat_timeout_seconds)
        if settings.task_heartbeat_interval_seconds > 0
        # Heartbeats disabled: only reap tasks that outlived any possible timeout.
        else MAX_TASK_TIMEOUT + timedelta(minutes=10)
    )
    return SchedulerConfig(
        batch_size=settings.scheduler_batch_size,
        cron_max_catchup_runs=settings.cron_max_catchup_runs,
        queued_timeout=timedelta(seconds=settings.reaper_queued_timeout_seconds),
        heartbeat_timeout=heartbeat_timeout,
    )


def build_scheduler(
    settings: Settings,
    *,
    session_factory: sessionmaker[Session],
    dispatcher: TaskDispatcher,
    clock: Clock,
    queue_for: Callable[[str], str],
) -> Scheduler:
    jobs = SchedulerJobs(
        session_factory=session_factory,
        clock=clock,
        orchestrator=Orchestrator(clock=clock),
        queue_for=queue_for,
        config=scheduler_config(settings),
    )
    return Scheduler(jobs=jobs, dispatcher=dispatcher, tick_seconds=settings.scheduler_tick_seconds)
