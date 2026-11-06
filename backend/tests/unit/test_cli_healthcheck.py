from __future__ import annotations

import pytest

from nuroflow.cli import healthcheck

TICK = "nuroflow_scheduler_last_tick_timestamp_seconds 1000.0\n"


def test_api_and_worker_are_healthy_when_their_endpoint_answers() -> None:
    assert healthcheck("api", fetch=lambda url: "{}")[0]
    assert healthcheck("worker", fetch=lambda url: "")[0]


def test_unreachable_endpoint_is_unhealthy() -> None:
    def refuse(url: str) -> str:
        raise ConnectionRefusedError

    healthy, detail = healthcheck("api", fetch=refuse)
    assert not healthy
    assert "unreachable" in detail


@pytest.mark.parametrize(("now", "healthy"), [(1030.0, True), (1061.0, False)])
def test_scheduler_must_have_ticked_recently(now: float, healthy: bool) -> None:
    # A stuck scheduler still answers HTTP; only a fresh tick proves the loop is alive.
    assert healthcheck("scheduler", fetch=lambda url: TICK, now=lambda: now)[0] is healthy


def test_scheduler_without_any_tick_is_unhealthy() -> None:
    assert healthcheck("scheduler", fetch=lambda url: "")[1] == "scheduler: no tick recorded yet"
