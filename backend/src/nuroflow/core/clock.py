"""Injectable time source.

Services never call ``datetime.now()`` directly. They receive a ``Clock``. Tests then
pin time with :class:`FrozenClock` to test expiry, token rotation windows and
(Phase 4) cron firing deterministically, without ``sleep()`` or monkeypatching.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Test clock that only moves when told to."""

    def __init__(self, at: datetime | None = None) -> None:
        self._now = at or datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        if self._now.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        self._now += timedelta(**kwargs)
