"""RFC 9457 Problem Details: one error format for every failure.

Example response (``Content-Type: application/problem+json``)::

    {
      "type": "https://nuroflow.dev/problems/invalid_credentials",
      "title": "Invalid email or password",
      "status": 401,
      "detail": "Invalid email or password",
      "code": "invalid_credentials",
      "instance": "/api/v1/auth/login",
      "request_id": "0193f0c2a4b87c1e9d3f4a5b6c7d8e9f"
    }

Security rules enforced here:

* Validation errors never echo the submitted ``input`` (it may contain a password).
* Unhandled exceptions return a generic 500. The traceback goes to the logs, never to
  the client.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from nuroflow.core.exceptions import AuthenticationError, NuroFlowError, RateLimited
from nuroflow.core.logging import request_id_ctx

logger = logging.getLogger(__name__)

PROBLEM_JSON = "application/problem+json"
PROBLEM_BASE = "https://nuroflow.dev/problems/"


def problem_response(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": PROBLEM_BASE + code,
        "title": title,
        "status": status,
        "detail": detail or title,
        "code": code,
        "instance": request.url.path,
        "request_id": request_id_ctx.get(),
        **extra,
    }
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON, headers=headers)


async def _handle_app_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, NuroFlowError)
    headers: dict[str, str] = {}
    if isinstance(exc, AuthenticationError):
        headers["WWW-Authenticate"] = 'Bearer error="invalid_token"'
    if isinstance(exc, RateLimited):
        headers["Retry-After"] = str(exc.retry_after)
    if exc.status_code >= 500:
        logger.error("application error", extra={"code": exc.code, "detail": exc.detail})
    return problem_response(
        request,
        status=exc.status_code,
        code=exc.code,
        title=exc.title,
        detail=exc.detail,
        headers=headers or None,
        **exc.extra,
    )


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    errors = [
        {"loc": list(err.get("loc", ())), "msg": err.get("msg"), "type": err.get("type")}
        for err in exc.errors()
    ]
    return problem_response(
        request,
        status=422,
        code="validation_error",
        title="Validation failed",
        detail="The request body or parameters are invalid",
        errors=errors,
    )


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    return problem_response(
        request,
        status=exc.status_code,
        code=code,
        title=str(exc.detail),
        headers=dict(exc.headers) if exc.headers else None,
    )


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled exception", exc_info=exc)
    return problem_response(
        request,
        status=500,
        code="internal_error",
        title="Internal server error",
        detail="An unexpected error occurred. Quote the request_id when reporting it.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(NuroFlowError, _handle_app_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected)
