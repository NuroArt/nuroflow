"""System-state gauges computed from Postgres when ``/metrics`` is scraped.

Why query the database instead of counting in-process? Queue depth, active runs and
scheduler lag are *global* facts. No single process knows them, but Postgres (the
source of truth, ADR-0001) does. Each query hits an index, and results are cached
for a few seconds so several Prometheus replicas scraping at once don't multiply load.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from nuroflow.domain.enums import RunStatus
from nuroflow.observability import metrics as m

_ACTIVE = [s.value for s in RunStatus if not s.is_terminal]

_QUERIES = {
    "queue": sa.text(
        "SELECT queue, count(*) AS n, min(queued_at) AS oldest "
        "FROM task_runs WHERE status = 'queued' GROUP BY queue"
    ),
    "runs": sa.text(
        "SELECT status, count(*) AS n FROM workflow_runs "
        "WHERE status = ANY(:active) GROUP BY status"
    ),
    "lag": sa.text(
        "SELECT min(t.next_fire_at) FROM workflow_triggers t "
        "JOIN workflows w ON w.id = t.workflow_id "
        "WHERE t.type = 'cron' AND t.is_enabled AND NOT w.is_paused AND w.deleted_at IS NULL"
    ),
    "outbox": sa.text("SELECT count(*) FROM notification_deliveries WHERE status = 'pending'"),
}


class DbGaugeRefresher:
    def __init__(
        self, engine: AsyncEngine, *, queues: list[str] | None = None, ttl_seconds: float = 5.0
    ) -> None:
        self._engine = engine
        self._ttl = ttl_seconds
        self._fresh_until = 0.0
        self._lock = asyncio.Lock()
        # Known queues always report (0 when idle): a missing series reads as "broken".
        self._seen_queues: set[str] = set(queues or [])

    async def refresh(self, now: datetime) -> None:
        if time.monotonic() < self._fresh_until:
            return
        async with self._lock:  # concurrent scrapes share one refresh
            if time.monotonic() < self._fresh_until:
                return
            await self._query(now)
            self._fresh_until = time.monotonic() + self._ttl

    async def _query(self, now: datetime) -> None:
        async with self._engine.connect() as conn:
            queues = (await conn.execute(_QUERIES["queue"])).all()
            runs = (await conn.execute(_QUERIES["runs"], {"active": _ACTIVE})).all()
            next_fire = await conn.scalar(_QUERIES["lag"])
            pending = await conn.scalar(_QUERIES["outbox"])

        current = {row.queue for row in queues}
        for queue in self._seen_queues - current:  # drained queues report 0, not stale values
            m.QUEUE_DEPTH.labels(queue=queue).set(0)
        self._seen_queues |= current
        oldest = None
        for row in queues:
            m.QUEUE_DEPTH.labels(queue=row.queue).set(row.n)
            if row.oldest is not None and (oldest is None or row.oldest < oldest):
                oldest = row.oldest
        m.OLDEST_QUEUED.set((now - oldest).total_seconds() if oldest else 0)

        counts = {row.status: row.n for row in runs}
        for status in _ACTIVE:
            m.ACTIVE_RUNS.labels(status=status).set(counts.get(status, 0))

        m.SCHEDULER_LAG.set(max(0.0, (now - next_fire).total_seconds()) if next_fire else 0)
        m.NOTIFICATIONS_PENDING.set(pending or 0)
