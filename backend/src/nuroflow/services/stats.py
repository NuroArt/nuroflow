"""Dashboard overview: run outcomes, latency percentiles, throughput and hot spots."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from nuroflow.core.clock import Clock
from nuroflow.core.exceptions import PermissionDenied
from nuroflow.domain.auth import Principal, Scope
from nuroflow.domain.enums import RunStatus
from nuroflow.repositories.interfaces import WorkflowUnitOfWork


class StatsService:
    def __init__(self, uow: WorkflowUnitOfWork, *, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def overview(self, principal: Principal, *, hours: int) -> dict[str, Any]:
        if not principal.has(Scope.RUNS_READ):
            raise PermissionDenied("Missing required scope: runs:read", scope="runs:read")
        now = self._clock.now()
        bucket = "hour" if hours <= 72 else "day"
        step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
        since = _floor(now - timedelta(hours=hours), bucket)
        raw = await self._uow.runs.overview(principal.user_id, since=since, bucket=bucket)

        by_status = {s.value: raw["by_status"].get(s.value, 0) for s in RunStatus}
        finished = by_status["succeeded"] + by_status["failed"]
        # Zero-fill: a chart with gaps for quiet hours misleads more than it informs.
        slots: dict[datetime, dict[str, int]] = {}
        cursor = since
        while cursor <= now:
            slots[cursor] = {"succeeded": 0, "failed": 0, "other": 0}
            cursor += step
        for slot, status, count in raw["series"]:
            key = status if status in ("succeeded", "failed") else "other"
            slots.setdefault(slot, {"succeeded": 0, "failed": 0, "other": 0})[key] += count
        return {
            "since": since,
            "bucket": bucket,
            "runs": sum(by_status.values()),
            "by_status": by_status,
            # Cancelled runs are excluded: cancelling isn't a failure of the system.
            "success_rate": round(by_status["succeeded"] / finished, 4) if finished else None,
            "duration_p50_seconds": _round(raw["p50"]),
            "duration_p95_seconds": _round(raw["p95"]),
            "active_runs": raw["active_runs"],
            "queued_tasks": raw["queued_tasks"],
            "series": [{"at": at, **counts} for at, counts in sorted(slots.items())],
            "failing_workflows": raw["failing"],
        }


def _floor(at: datetime, bucket: str) -> datetime:
    at = at.replace(minute=0, second=0, microsecond=0)
    return at.replace(hour=0) if bucket == "day" else at


def _round(value: float | None) -> float | None:
    return round(float(value), 3) if value is not None else None
