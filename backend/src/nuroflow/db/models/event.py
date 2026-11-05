"""Inbound events, e.g. ``lead.captured``, that fan out to event triggers."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from nuroflow.db.base import EMPTY_JSON_OBJECT, Base
from nuroflow.db.mixins import UUIDPrimaryKeyMixin


class Event(UUIDPrimaryKeyMixin, Base):
    """An immutable inbound fact. ``idempotency_key`` deduplicates client retries."""

    __tablename__ = "events"
    __table_args__ = (
        sa.UniqueConstraint("owner_id", "idempotency_key"),
        sa.Index("ix_events_owner_id_type_received_at", "owner_id", "type", "received_at"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(sa.String(200))
    source: Mapped[str | None] = mapped_column(sa.String(200))
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT)
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(255))
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("api_keys.id", ondelete="SET NULL")
    )
    received_at: Mapped[datetime] = mapped_column(server_default=sa.func.now())
    processed_at: Mapped[datetime | None]
