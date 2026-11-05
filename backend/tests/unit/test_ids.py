from __future__ import annotations

import time
import uuid

import pytest

from nuroflow.core.ids import uuid7, uuid7_timestamp_ms


def test_uuid7_sets_version_and_rfc_variant() -> None:
    value = uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_uuid7_embeds_current_unix_milliseconds() -> None:
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000
    # +1 allows for counter-overflow borrowing within a single millisecond.
    assert before <= uuid7_timestamp_ms(value) <= after + 1


def test_uuid7_is_strictly_monotonic_under_burst() -> None:
    ids = [uuid7() for _ in range(20_000)]  # forces many IDs per millisecond
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_uuid7_timestamp_rejects_other_versions() -> None:
    with pytest.raises(ValueError, match="version 7"):
        uuid7_timestamp_ms(uuid.uuid4())
