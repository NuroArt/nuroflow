"""Pure Phase 4 logic: DST-safe cron math, trigger identity, secrets, signatures, config."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from nuroflow.core.config import Settings
from nuroflow.core.crypto import DecryptionError, SecretBox
from nuroflow.domain.schedule import due_slots, next_fire_after
from nuroflow.domain.workflow_spec import WorkflowSpec
from nuroflow.services.ingest import sign_webhook

NY = "America/New_York"


def local(expr: str, start: datetime, n: int) -> list[str]:
    out, t = [], start
    for _ in range(n):
        t = next_fire_after(expr, NY, t)
        out.append(t.astimezone(ZoneInfo(NY)).strftime("%m-%d %H:%M %Z"))
    return out


# --------------------------------------------------------------------------- DST
def test_daily_job_fires_once_when_clocks_fall_back() -> None:
    # 2026-11-01 01:30 happens twice in New York (EDT, then EST).
    assert local("30 1 * * *", datetime(2026, 10, 31, 16, tzinfo=UTC), 3) == [
        "11-01 01:30 EDT",
        "11-02 01:30 EST",
        "11-03 01:30 EST",
    ]


def test_hourly_job_fires_in_both_repeated_hours() -> None:
    assert local("0 * * * *", datetime(2026, 11, 1, 4, 30, tzinfo=UTC), 3) == [
        "11-01 01:00 EDT",
        "11-01 01:00 EST",  # a genuinely different hour: one hour later in real time
        "11-01 02:00 EST",
    ]


def test_job_in_skipped_hour_runs_late_not_never() -> None:
    # 2026-03-08 02:30 doesn't exist in New York (02:00 → 03:00).
    assert local("30 2 * * *", datetime(2026, 3, 7, 17, tzinfo=UTC), 2) == [
        "03-08 03:00 EDT",
        "03-09 02:30 EDT",
    ]


def test_schedule_is_strictly_increasing_across_a_whole_year() -> None:
    t = datetime(2026, 1, 1, tzinfo=UTC)
    for _ in range(800):
        nxt = next_fire_after("15 1,13 * * *", NY, t)
        assert nxt > t
        t = nxt


def test_due_slots_is_bounded_and_inclusive() -> None:
    first = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    slots = list(
        due_slots("*/10 * * * *", "UTC", first=first, until=first + timedelta(minutes=30), limit=10)
    )
    assert [s.minute for s in slots] == [0, 10, 20, 30]
    assert (
        len(
            list(
                due_slots("* * * * *", "UTC", first=first, until=first + timedelta(days=1), limit=5)
            )
        )
        == 5
    )


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        next_fire_after("* * * * *", "UTC", datetime(2026, 1, 1))


# --------------------------------------------------------------------------- trigger identity
def spec_with(*triggers: dict[str, object]) -> WorkflowSpec:
    return WorkflowSpec.model_validate(
        {
            "slug": "s",
            "name": "n",
            "tasks": [{"key": "a", "operator": "noop"}],
            "triggers": list(triggers),
        }
    )


def test_trigger_keys_identify_the_trigger_not_its_settings() -> None:
    a = spec_with({"type": "cron", "cron": "0 9 * * 1", "catchup": False}).triggers[0]
    b = spec_with({"type": "cron", "cron": "0 9 * * 1", "catchup": True}).triggers[0]
    assert a.spec_key == b.spec_key == "cron:0 9 * * 1:UTC"  # catchup is a setting, not identity
    e1 = spec_with(
        {"type": "event", "event_type": "lead.captured", "event_filter": {"a": 1, "b": 2}}
    )
    e2 = spec_with(
        {"type": "event", "event_type": "lead.captured", "event_filter": {"b": 2, "a": 1}}
    )
    assert e1.triggers[0].spec_key == e2.triggers[0].spec_key  # key order doesn't matter
    assert spec_with({"type": "webhook"}).triggers[0].spec_key == "webhook:default"


def test_duplicate_triggers_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate triggers"):
        spec_with({"type": "webhook"}, {"type": "webhook", "name": "default"})


# --------------------------------------------------------------------------- secrets
def test_secret_box_round_trip_and_tamper_detection() -> None:
    box = SecretBox([Fernet.generate_key()])
    token = box.encrypt("whsec_123")
    assert box.decrypt(token) == "whsec_123"
    tampered = token[:-4] + (b"AAAA" if not token.endswith(b"AAAA") else b"BBBB")
    with pytest.raises(DecryptionError):
        box.decrypt(tampered)


def test_secret_box_key_rotation() -> None:
    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    legacy = SecretBox([old_key]).encrypt("s3cret")
    rotated_box = SecretBox([new_key, old_key])  # newest first
    assert rotated_box.decrypt(legacy) == "s3cret"  # old ciphertexts still readable
    migrated = rotated_box.rotate(legacy)
    assert SecretBox([new_key]).decrypt(migrated) == "s3cret"  # old key can now be retired


def test_secret_box_from_settings() -> None:
    derived = SecretBox.from_settings(Settings(_env_file=None, secret_key="k" * 48))  # type: ignore[call-arg, arg-type]
    again = SecretBox.from_settings(Settings(_env_file=None, secret_key="k" * 48))  # type: ignore[call-arg, arg-type]
    assert again.decrypt(derived.encrypt("x")) == "x"  # deterministic across restarts
    explicit_key = Fernet.generate_key()
    explicit = SecretBox.from_settings(
        Settings(_env_file=None, encryption_keys=[SecretStr(explicit_key.decode())])  # type: ignore[call-arg]
    )
    assert SecretBox([explicit_key]).decrypt(explicit.encrypt("y")) == "y"
    assert base64.urlsafe_b64decode(explicit_key)  # sanity: a real Fernet key


def test_webhook_signature_format() -> None:
    sig = sign_webhook("whsec_x", "1700000000", b'{"a":1}')
    assert sig.startswith("sha256=")
    assert len(sig) == len("sha256=") + 64
    assert sig != sign_webhook("whsec_x", "1700000001", b'{"a":1}')  # timestamp is signed too


# --------------------------------------------------------------------------- configuration
def test_reaper_threshold_must_exceed_heartbeat_interval() -> None:
    with pytest.raises(ValueError, match="at least 3x"):
        Settings(  # type: ignore[call-arg]
            _env_file=None, task_heartbeat_interval_seconds=30, reaper_heartbeat_timeout_seconds=60
        )
    Settings(_env_file=None, task_heartbeat_interval_seconds=0, reaper_heartbeat_timeout_seconds=5)  # type: ignore[call-arg]
