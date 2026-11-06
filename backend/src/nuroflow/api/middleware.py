"""Request context middleware: request IDs, access logging, security headers.

Written as **pure ASGI** rather than ``BaseHTTPMiddleware``. The latter wraps
every response in an extra task, breaks contextvars propagation in some cases, and
buffers streaming responses, which would break the SSE log tail in Phase 6.
"""

from __future__ import annotations

import logging
import re
import time

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nuroflow.core.ids import uuid7
from nuroflow.core.logging import request_id_ctx
from nuroflow.observability.metrics import HTTP_DURATION, HTTP_REQUESTS

logger = logging.getLogger("nuroflow.access")

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = _header(scope, b"x-request-id")
        request_id = incoming if incoming and _VALID_REQUEST_ID.match(incoming) else uuid7().hex
        token = request_id_ctx.set(request_id)
        path: str = scope.get("path", "")
        started = time.perf_counter()
        status_holder = {"status": 500}
        streaming = False

        async def send_wrapper(message: Message) -> None:
            nonlocal streaming
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = MutableHeaders(scope=message)
                streaming = headers.get("content-type", "").startswith("text/event-stream")
                headers["X-Request-ID"] = request_id
                for name, value in _SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                if not path.startswith(_DOCS_PATHS):
                    # The API serves JSON only: forbid every resource type.
                    headers.setdefault(
                        "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
                    )
                    # Responses carry tokens and user data: never cache them in shared caches.
                    headers.setdefault("Cache-Control", "no-store")
                if self.hsts:
                    headers.setdefault(
                        "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
                    )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - started
            duration_ms = round(elapsed * 1000, 2)
            # Route *template*, never the raw path: bounded label cardinality.
            route = route_template(scope)
            method = scope.get("method", "")
            HTTP_REQUESTS.labels(method=method, route=route, status=status_holder["status"]).inc()
            if not streaming:  # a 30-minute SSE stream isn't "latency"
                HTTP_DURATION.labels(method=method, route=route).observe(elapsed)
            client = scope.get("client")
            if path not in ("/healthz", "/readyz", "/metrics"):  # keep probe noise out of logs
                logger.info(
                    "%s %s %s %.2fms",
                    scope.get("method"),
                    path,
                    status_holder["status"],
                    duration_ms,
                    extra={
                        "http_method": scope.get("method"),
                        "path": path,
                        "status": status_holder["status"],
                        "duration_ms": duration_ms,
                        "client_ip": client[0] if client else None,
                    },
                )
            request_id_ctx.reset(token)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            decoded: str = value.decode("latin-1")
            return decoded
    return None


def route_template(scope: Scope) -> str:
    """The matched route's template including any ``include_router`` prefix, e.g.
    ``/api/v1/runs/{run_id}``; ``"unmatched"`` for 404s.

    FastAPI includes routers lazily, so the matched route only knows its *own* path
    (``/runs/{run_id}``). The mount prefix is the part of the real URL before the
    suffix that the route's regex matches. Prefixes are static, so this is exact.
    """
    route = scope.get("route")
    template = getattr(route, "path", None)
    regex = getattr(route, "path_regex", None)
    if not template or regex is None:
        return "unmatched"
    path: str = scope.get("path", "")
    for i, char in enumerate(path):
        if char == "/" and regex.fullmatch(path[i:]):
            return path[:i] + str(template)
    return str(template)
