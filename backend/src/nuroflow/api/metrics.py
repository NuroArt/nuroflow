"""``GET /metrics``: Prometheus exposition for the API process.

Unversioned, like the health probes: it belongs to the platform, not the public API.
Locally it's open. When ``NUROFLOW_METRICS_TOKEN`` is set (required when deployed),
scrapers must send ``Authorization: Bearer <token>``. Metrics reveal traffic shape,
queue sizes and LLM spend, which is business information.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

from nuroflow.api.deps import Container
from nuroflow.core.exceptions import AuthenticationError, NotFound

router = APIRouter(tags=["health"])


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request, container: Container) -> Response:
    settings = container.settings
    if not settings.metrics_enabled:
        raise NotFound()
    if settings.metrics_token is not None:
        expected = f"Bearer {settings.metrics_token.get_secret_value()}"
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), expected.encode()):  # constant time
            raise AuthenticationError("Valid metrics token required")
    await container.db_gauges.refresh(container.clock.now())
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
