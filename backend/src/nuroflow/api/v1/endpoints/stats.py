"""/stats/overview: the numbers behind the dashboard's home page."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from nuroflow.api.deps import Container, CurrentPrincipal, UoW
from nuroflow.api.v1.schemas.stats import Overview
from nuroflow.services.stats import StatsService

router = APIRouter(tags=["runs"])


def get_stats_service(uow: UoW, container: Container) -> StatsService:
    return StatsService(uow, clock=container.clock)


@router.get("/stats/overview", response_model=Overview)
async def overview(
    principal: CurrentPrincipal,
    stats: Annotated[StatsService, Depends(get_stats_service)],
    hours: Annotated[int, Query(ge=1, le=720)] = 24,
) -> Overview:
    """Run outcomes, p50/p95 duration and throughput over the last ``hours``
    (hourly buckets up to 72h, daily beyond)."""
    return Overview.model_validate(await stats.overview(principal, hours=hours))
