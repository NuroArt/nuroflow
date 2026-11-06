"""FastAPI application factory.

``create_app()`` is the only way an app instance is built:

* Production: ``create_app()`` reads settings from the environment, and the lifespan
  builds the container and tears it down on shutdown.
* Tests: ``create_app(settings, container=...)`` injects a pre-built container
  (throwaway database, in-memory rate limiter, frozen clock). No monkeypatching.

Middleware order matters. Starlette runs the *last added* first, so the request
context (request ID, logging, security headers) wraps everything, CORS included.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from nuroflow import __version__
from nuroflow.api.container import AppContainer, build_container
from nuroflow.api.errors import register_exception_handlers
from nuroflow.api.health import router as health_router
from nuroflow.api.metrics import router as metrics_router
from nuroflow.api.middleware import RequestContextMiddleware
from nuroflow.api.v1.router import api_router
from nuroflow.core.config import Settings, get_settings
from nuroflow.core.logging import configure_logging

logger = logging.getLogger(__name__)

OPENAPI_TAGS = [
    {"name": "auth", "description": "Login, token refresh and session management."},
    {"name": "users", "description": "Administrator user management."},
    {"name": "api-keys", "description": "Machine credentials for integrations and webhooks."},
    {"name": "workflows", "description": "Workflow definitions, versions and manual runs."},
    {"name": "runs", "description": "Run history, task attempts, logs and stored records."},
    {"name": "operators", "description": "Installed task types and their input schemas."},
    {"name": "triggers", "description": "Events, signed webhooks and trigger state."},
    {"name": "health", "description": "Liveness and readiness probes."},
]


def create_app(
    settings: Settings | None = None, *, container: AppContainer | None = None
) -> FastAPI:
    settings = settings or (container.settings if container else get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_container = container is None
        app.state.container = container or build_container(settings)
        logger.info(
            "nuroflow api starting",
            extra={"version": __version__, "environment": settings.environment.value},
        )
        try:
            yield
        finally:
            if owns_container:
                await app.state.container.aclose()
            logger.info("nuroflow api stopped")

    app = FastAPI(
        title=f"{settings.app_name} API",
        version=__version__,
        summary="Durable, AI-native workflow orchestration.",
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    if container is not None:
        # Available even when the ASGI lifespan isn't run (e.g. httpx.ASGITransport).
        app.state.container = container

    register_exception_handlers(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-API-Key",
            "X-Request-ID",
            "Idempotency-Key",
        ],
        expose_headers=["X-Request-ID", "Retry-After"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware, hsts=settings.environment.is_deployed)

    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn --factory nuroflow.api.main:app_factory``."""
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    return create_app(settings)
