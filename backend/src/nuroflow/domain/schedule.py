"""Cron schedule math in a trigger's IANA timezone, DST-safe. Pure functions.

Daylight-saving transitions are where naive schedulers go wrong:

* **Spring forward** (02:00 → 03:00): a job at 02:30 has no 02:30 that day. croniter
  fires it at 03:00 the same day, which is what users expect: it runs once, late,
  and isn't skipped.
* **Fall back** (02:00 → 01:00): 01:30 happens *twice*. croniter would fire a daily
  01:30 job twice. We follow Vixie-cron semantics instead: a job pinned to specific
  hours fires **once** at a repeated wall-clock time, while an every-hour job
  (``hour == *``) fires in both real hours, because they're genuinely different hours.

All inputs and outputs are timezone-aware UTC datetimes. Local time exists only inside
this module.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from croniter import croniter


def _hour_field_is_wildcard(expression: str) -> bool:
    return expression.split()[1] == "*"


def next_fire_after(expression: str, timezone: str, after: datetime) -> datetime:
    """The first scheduled instant strictly after ``after`` (UTC in, UTC out)."""
    if after.tzinfo is None:
        raise ValueError("after must be timezone-aware")
    zone = ZoneInfo(timezone)
    local_after = after.astimezone(zone)
    iterator = croniter(expression, local_after)
    candidate: datetime = iterator.get_next(datetime)
    if not _hour_field_is_wildcard(expression):
        # Fall-back dedupe: skip a candidate whose wall-clock time we already passed.
        while candidate.replace(tzinfo=None) == local_after.replace(tzinfo=None) or (
            candidate.fold == 1 and _same_wall_time_seen(candidate, after, zone)
        ):
            candidate = iterator.get_next(datetime)
    return candidate.astimezone(UTC)


def _same_wall_time_seen(candidate: datetime, after: datetime, zone: ZoneInfo) -> bool:
    """True if this wall-clock time already occurred at or before ``after`` (DST fold)."""
    first_occurrence = candidate.replace(fold=0).astimezone(UTC)
    return first_occurrence <= after and first_occurrence != candidate.astimezone(UTC)


def due_slots(
    expression: str, timezone: str, *, first: datetime, until: datetime, limit: int
) -> Iterator[datetime]:
    """Yield scheduled instants from ``first`` (itself a slot) up to ``until``, inclusive."""
    slot = first
    emitted = 0
    while slot <= until and emitted < limit:
        yield slot
        emitted += 1
        slot = next_fire_after(expression, timezone, slot)
