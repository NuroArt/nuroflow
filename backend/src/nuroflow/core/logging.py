"""Logging setup with request and execution correlation.

Plain text locally, single-line JSON when ``NUROFLOW_LOG_JSON=true``. Two contextvars
attach correlation fields to **every** record, including records from Celery,
SQLAlchemy and httpx, because they're added by a handler filter rather than by callers:

* ``request_id``: set by the API middleware, so one ``grep`` finds a whole request.
* ``log_context``: bound around a unit of work with :func:`bind_log_context`
  (``run_id``, ``task_run_id``, ``task_key``, ``operator``, ``attempt``) by the worker,
  and ``job`` by the scheduler. ``grep run_id=…`` then follows one run across processes.

Why not structlog (the original plan)? Every library we run logs through stdlib, and
this formatter already emits structured JSON. structlog would add a second logging
API with no change in output. The decision is recorded in docs/OBSERVABILITY.md.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
log_context_ctx: ContextVar[dict[str, Any]] = ContextVar("log_context", default={})  # noqa: B039

_RESERVED = set(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "request_id",
    "ctx_suffix",
}


@contextmanager
def bind_log_context(**fields: Any) -> Iterator[None]:
    """Attach ``fields`` to every log record emitted inside the block (nests, async-safe).

    The dict is replaced, never mutated, so the default ``{}`` is never shared state
    and an inner bind can't leak into the outer scope.
    """
    token = log_context_ctx.set({**log_context_ctx.get(), **fields})
    try:
        yield
    finally:
        log_context_ctx.reset(token)


class RequestIdFilter(logging.Filter):
    """Adds ``request_id`` and bound context fields. Explicit ``extra=`` values win."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get() or "-"
        context = log_context_ctx.get()
        for key, value in context.items():
            if not hasattr(record, key):
                setattr(record, key, value)
        record.ctx_suffix = (
            " " + " ".join(f"{k}={v}" for k, v in context.items()) if context else ""
        )
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(*, level: str = "INFO", json_output: bool = False) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        JsonFormatter()
        if json_output
        else logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s%(ctx_suffix)s"
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Uvicorn's own access log duplicates our structured one.
    logging.getLogger("uvicorn.access").disabled = True
    for noisy in ("sqlalchemy.engine", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))
