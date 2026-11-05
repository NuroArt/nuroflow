"""``http.request``: call any HTTP API, with SSRF protection and retry classification.

SSRF (server-side request forgery) is the #1 risk of a "call any URL" feature. A
workflow author could point the worker at ``http://169.254.169.254/`` (cloud metadata
credentials) or at internal services. We resolve the hostname and refuse private,
loopback, link-local, reserved and multicast addresses unless the operator explicitly
allows private networks (local development only).

Known limitation: a DNS answer can change between our check and httpx's own lookup
(DNS rebinding). The production-grade fix is to connect to the vetted IP with the
original Host header / SNI, or to route egress through an allowlisting proxy.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import AnyHttpUrl, Field

from nuroflow.operators.base import (
    NonRetryableError,
    Operator,
    OperatorContext,
    OperatorInput,
    RetryableError,
)

MAX_BODY_CHARS = 64_000
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

Resolver = Callable[[str], list[str]]


def _system_resolver(host: str) -> list[str]:
    return sorted({str(info[4][0]) for info in socket.getaddrinfo(host, None)})


class HttpRequestInput(OperatorInput):
    url: AnyHttpUrl
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    json_body: Any = Field(default=None, alias="json")
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    expected_status: list[int] | None = Field(
        default=None, description="Statuses treated as success (default: any 2xx)."
    )


class HttpRequestOperator(Operator):
    name = "http.request"
    description = "Make an HTTP request; 429/5xx/timeouts are retried, other 4xx fail fast."
    input_model = HttpRequestInput

    def __init__(
        self,
        *,
        allow_private_networks: bool = False,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = _system_resolver,
    ) -> None:
        self._allow_private = allow_private_networks
        self._transport = transport
        self._resolve = resolver

    def execute(self, inputs: HttpRequestInput, ctx: OperatorContext) -> dict[str, Any]:
        url = str(inputs.url)
        self._guard_ssrf(url)
        headers = {"Idempotency-Key": ctx.idempotency_key, **inputs.headers}
        ctx.log(f"{inputs.method} {_redact(url)}")
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=inputs.timeout_seconds,
                follow_redirects=False,  # a redirect could bounce us to a private IP
            ) as client:
                response = client.request(
                    inputs.method,
                    url,
                    headers=headers,
                    params=inputs.params,
                    json=inputs.json_body,
                )
        except httpx.TimeoutException as exc:
            raise RetryableError(f"timeout after {inputs.timeout_seconds}s") from exc
        except httpx.TransportError as exc:
            raise RetryableError(f"connection error: {type(exc).__name__}") from exc

        ok = (
            response.status_code in inputs.expected_status
            if inputs.expected_status
            else response.is_success
        )
        body = _parse_body(response)
        if not ok:
            message = f"HTTP {response.status_code} from {_redact(url)}"
            if response.status_code in RETRYABLE_STATUS:
                raise RetryableError(message)
            raise NonRetryableError(message)
        return {
            "status_code": response.status_code,
            "headers": {k: v for k, v in response.headers.items() if k.lower() in _SAFE_HEADERS},
            "body": body,
        }

    def _guard_ssrf(self, url: str) -> None:
        if self._allow_private:
            return
        host = urlsplit(url).hostname or ""
        try:
            addresses = self._resolve(host)
        except OSError as exc:
            raise RetryableError(f"DNS resolution failed for {host}") from exc
        for raw in addresses:
            ip = ipaddress.ip_address(raw)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            ):
                raise NonRetryableError(
                    f"refusing to call {host}: resolves to non-public address {ip}"
                )


_SAFE_HEADERS = frozenset({"content-type", "etag", "location", "retry-after", "x-request-id"})


def _parse_body(response: httpx.Response) -> Any:
    if "json" in response.headers.get("content-type", ""):
        try:
            return response.json()
        except ValueError:
            pass
    return response.text[:MAX_BODY_CHARS]


def _redact(url: str) -> str:
    """Drop query strings from logs (they often carry API keys)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"
