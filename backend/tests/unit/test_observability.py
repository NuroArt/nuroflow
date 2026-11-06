"""Log context, the SSE loop and metric recording, all without I/O.

The SSE loop takes ``sleep`` and ``monotonic`` as parameters, so heartbeats and the
maximum stream duration are tested in microseconds with a fake clock.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator

import pytest
from prometheus_client import REGISTRY

from nuroflow.api.sse import SSEEvent, event_stream
from nuroflow.core.logging import (
    JsonFormatter,
    RequestIdFilter,
    bind_log_context,
    configure_logging,
)
from nuroflow.engine.executor import record_attempt_metrics
from nuroflow.operators.base import OperatorContext


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def record(**extra: object) -> logging.LogRecord:
    rec = logging.makeLogRecord({"name": "t", "levelname": "INFO", "msg": "hi", **extra})
    RequestIdFilter().filter(rec)
    return rec


# ============================================================================ log context
def test_bound_context_appears_in_json_and_nests() -> None:
    with bind_log_context(run_id="r1"), bind_log_context(task_key="qualify", attempt=2):
        payload = json.loads(JsonFormatter().format(record()))
    assert payload["run_id"] == "r1"
    assert payload["task_key"] == "qualify"
    assert payload["attempt"] == 2


def test_inner_context_does_not_leak_outward() -> None:
    with bind_log_context(run_id="r1"):
        with bind_log_context(task_key="a"):
            pass
        rec = record()
    assert not hasattr(rec, "task_key")
    assert rec.run_id == "r1"  # type: ignore[attr-defined]
    assert not hasattr(record(), "run_id")


def test_explicit_extra_wins_over_bound_context() -> None:
    with bind_log_context(attempt=1):
        assert record(attempt=7).attempt == 7  # type: ignore[attr-defined]


def test_text_format_appends_context(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", json_output=False)
    with bind_log_context(run_id="r9", job="fire_cron_triggers"):
        logging.getLogger("nuroflow.test").info("tick")
    assert "tick run_id=r9 job=fire_cron_triggers" in capsys.readouterr().out


# ============================================================================ SSE
def test_sse_event_encoding_is_single_line_json() -> None:
    encoded = SSEEvent("log", {"message": "line1\nline2"}, id="42").encode()
    assert encoded == 'id: 42\nevent: log\ndata: {"message":"line1\\nline2"}\n\n'


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def collect(
    outcomes: list[tuple[list[SSEEvent], bool]],
    *,
    max_seconds: float = 100,
    disconnect_after: int | None = None,
) -> tuple[list[str], FakeTime]:
    clock = FakeTime()
    calls = {"n": 0}

    async def source() -> tuple[list[SSEEvent], bool]:
        calls["n"] += 1
        return outcomes.pop(0) if outcomes else ([], False)

    async def disconnected() -> bool:
        return disconnect_after is not None and calls["n"] >= disconnect_after

    chunks = [
        chunk
        async for chunk in event_stream(
            source,
            is_disconnected=disconnected,
            poll_seconds=1,
            max_seconds=max_seconds,
            heartbeat_seconds=15,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
    ]
    return chunks, clock


async def test_stream_sends_events_then_end_when_finished() -> None:
    chunks, _ = await collect(
        [([SSEEvent("log", {"n": 1}, id="1")], False), ([SSEEvent("log", {"n": 2}, id="2")], True)]
    )
    assert chunks[0] == "retry: 3000\n\n"
    assert [c.split("\n")[0] for c in chunks[1:3]] == ["id: 1", "id: 2"]
    assert chunks[-1] == 'event: end\ndata: {"reason":"finished"}\n\n'


async def test_stream_sends_heartbeats_while_idle_and_respects_max_duration() -> None:
    chunks, clock = await collect([], max_seconds=40)
    assert chunks.count(": keepalive\n\n") == 2  # at 15s and 30s
    assert chunks[-1] == 'event: end\ndata: {"reason":"max_duration"}\n\n'
    assert clock.now == 40


async def test_stream_stops_quietly_when_client_disconnects() -> None:
    chunks, clock = await collect([], disconnect_after=3)
    assert chunks == ["retry: 3000\n\n"]
    assert len(clock.sleeps) == 2


# ============================================================================ metrics
def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_attempt_metrics_include_llm_tokens_and_cost() -> None:
    ctx = OperatorContext(
        workflow_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        task_run_id=uuid.uuid4(),
        task_key="qualify",
        owner_id=uuid.uuid4(),
        attempt=1,
        timeout_seconds=30,
    )
    ctx.metrics["llm"] = {
        "provider": "anthropic",
        "model": "claude-test",
        "input_tokens": 120,
        "output_tokens": 30,
        "cost_usd": 0.002,
    }
    llm = {"provider": "anthropic", "model": "claude-test"}
    before = {
        "attempts": sample(
            "nuroflow_task_attempts_total", operator="ai.structured", outcome="succeeded"
        ),
        "input": sample("nuroflow_llm_tokens_total", **llm, kind="input"),
        "cost": sample("nuroflow_llm_cost_usd_total", **llm),
        "count": sample("nuroflow_task_duration_seconds_count", operator="ai.structured"),
    }
    record_attempt_metrics("ai.structured", "succeeded", 1500, ctx)
    after_attempts = sample(
        "nuroflow_task_attempts_total", operator="ai.structured", outcome="succeeded"
    )
    assert after_attempts - before["attempts"] == 1
    assert sample("nuroflow_llm_tokens_total", **llm, kind="input") - before["input"] == 120
    assert sample("nuroflow_llm_cost_usd_total", **llm) - before["cost"] == pytest.approx(0.002)
    count = sample("nuroflow_task_duration_seconds_count", operator="ai.structured")
    assert count - before["count"] == 1
