from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from nuroflow.core.logging import JsonFormatter, RequestIdFilter, configure_logging, request_id_ctx


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def make_record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.makeLogRecord({"name": "test", "levelname": "INFO", "msg": msg, **extra})
    RequestIdFilter().filter(record)
    return record


def test_json_formatter_includes_request_id_and_extras() -> None:
    token = request_id_ctx.set("req-123")
    try:
        line = JsonFormatter().format(make_record(user_id="u-1"))
    finally:
        request_id_ctx.reset(token)
    payload = json.loads(line)
    assert payload["msg"] == "hello"
    assert payload["request_id"] == "req-123"
    assert payload["user_id"] == "u-1"
    assert payload["level"] == "info"


def test_json_formatter_renders_exceptions() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = make_record(exc_info=sys.exc_info())
    assert "ValueError: boom" in json.loads(JsonFormatter().format(record))["exc"]


def test_request_id_defaults_to_dash_outside_requests() -> None:
    assert make_record().request_id == "-"  # type: ignore[attr-defined]


@pytest.mark.parametrize("json_output", [True, False])
def test_configure_logging_installs_single_handler(json_output: bool) -> None:
    configure_logging(level="DEBUG", json_output=json_output)
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert root.level == logging.DEBUG
    assert isinstance(root.handlers[0].formatter, JsonFormatter) is json_output
    assert logging.getLogger("uvicorn.access").disabled
