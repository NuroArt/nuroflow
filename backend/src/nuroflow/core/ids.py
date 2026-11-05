"""Time-ordered UUIDv7 identifiers (RFC 9562).

Why not ``uuid.uuid4``? Random keys scatter inserts across B-tree pages, causing page
splits and index bloat on write-heavy tables such as ``task_runs``. UUIDv7 starts with
a 48-bit Unix millisecond timestamp, so new keys land at the right edge of the index,
just like a serial, without leaking row counts. See ADR-0006.

Python 3.14 ships ``uuid.uuid7``; this module can be swapped for it once 3.14 is the
minimum supported version.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_counter = 0
_COUNTER_BITS = 12  # the rand_a field, used as a monotonic counter within one millisecond
_COUNTER_MAX = (1 << _COUNTER_BITS) - 1


def uuid7() -> uuid.UUID:
    """Return a new UUIDv7, monotonic within this process.

    Layout (128 bits)::

        unix_ts_ms (48) | ver=7 (4) | counter (12) | var=0b10 (2) | random (62)

    Within one millisecond, the 12-bit counter keeps IDs strictly increasing (RFC 9562
    §6.2, "Fixed Bit-Length Dedicated Counter"). If the counter overflows, we borrow the
    next millisecond rather than lose ordering.
    """
    global _last_ms, _counter

    with _lock:
        now_ms = time.time_ns() // 1_000_000
        if now_ms > _last_ms:
            _last_ms = now_ms
            _counter = int.from_bytes(os.urandom(2), "big") & 0x3FF  # random start, headroom
        else:
            _counter += 1
            if _counter > _COUNTER_MAX:
                _last_ms += 1
                _counter = 0
        ts_ms, counter = _last_ms, _counter

    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ts_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= counter << 64
    value |= 0b10 << 62
    value |= rand_b
    return uuid.UUID(int=value)


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    """Extract the embedded Unix millisecond timestamp from a UUIDv7."""
    if value.version != 7:
        raise ValueError(f"expected a version 7 UUID, got version {value.version}")
    return value.int >> 80
