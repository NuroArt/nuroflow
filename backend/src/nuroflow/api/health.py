"""Liveness and readiness probes (unversioned: they belong to the platform, not the API).

* ``/healthz``: is the process alive? Never touches dependencies. If this fails, the
  orchestrator restarts the container.
* ``/readyz``: can this instance serve traffic right now? Checks Postgres and Redis
  with tight timeouts. If this fails, the load balancer stops routing here, but the
  container is *not* restarted (restarting won't fix a database outage).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from nuroflow import __version__
from nuroflow.api.deps import Container

router = APIRouter(tags=["health"])

CHECK_TIMEOUT_SECONDS = 2.0


class CheckResult(BaseModel):
    status: Literal["ok", "error"]
    latency_ms: float
    error: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "error"]
    version: str
    checks: dict[str, CheckResult] = {}


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@router.get(
    "/readyz",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "A dependency is unavailable"}},
)
async def readyz(container: Container, response: Response) -> HealthResponse:
    async def database() -> None:
        async with container.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def redis() -> None:
        await container.redis.ping()

    checks = dict(
        zip(
            ("database", "redis"),
            await asyncio.gather(_timed(database()), _timed(redis())),
            strict=True,
        )
    )
    healthy = all(c.status == "ok" for c in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(status="ok" if healthy else "error", version=__version__, checks=checks)


async def _timed(check: Awaitable[None]) -> CheckResult:
    started = time.perf_counter()
    try:
        await asyncio.wait_for(check, timeout=CHECK_TIMEOUT_SECONDS)
    except Exception as exc:
        return CheckResult(
            status="error",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            error=type(exc).__name__,  # class name only: never leak connection strings
        )
    return CheckResult(status="ok", latency_ms=round((time.perf_counter() - started) * 1000, 2))
