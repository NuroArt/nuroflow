"""Notifications: where alerts go, which events trigger them, and delivery history."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from nuroflow.db.base import EMPTY_JSON_OBJECT, Base, str_enum
from nuroflow.db.mixins import CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from nuroflow.domain.enums import ChannelType, DeliveryStatus, RunEvent

_RUN_EVENT_VALUES = ", ".join(f"'{e.value}'" for e in RunEvent)


class NotificationChannel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A destination such as a Telegram chat (via a ``telegram`` connection) or a webhook URL."""

    __tablename__ = "notification_channels"
    __table_args__ = (sa.UniqueConstraint("owner_id", "name"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(sa.String(100))
    type: Mapped[ChannelType] = mapped_column(str_enum(ChannelType, name="type"))
    # A connection still in use by a channel can't be deleted.
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("connections.id", ondelete="RESTRICT"), index=True
    )
    target: Mapped[str] = mapped_column(sa.String(500))  # chat_id or URL
    is_enabled: Mapped[bool] = mapped_column(server_default=sa.true())


class NotificationSubscription(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Workflow → channel routing: 'send run.failed for workflow X to channel Y'."""

    __tablename__ = "notification_subscriptions"
    __table_args__ = (
        sa.UniqueConstraint("workflow_id", "channel_id"),
        sa.CheckConstraint("cardinality(events) > 0", name="events_not_empty"),
        sa.CheckConstraint(f"events <@ ARRAY[{_RUN_EVENT_VALUES}]::text[]", name="events_valid"),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workflows.id", ondelete="CASCADE")
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("notification_channels.id", ondelete="CASCADE"), index=True
    )
    events: Mapped[list[str]]
    is_enabled: Mapped[bool] = mapped_column(server_default=sa.true())


class NotificationDelivery(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Audit + retry state for every notification send (an outbox for alerts)."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        sa.Index(
            "ix_notification_deliveries_pending_due",
            "next_attempt_at",
            postgresql_where=sa.text("status = 'pending'"),
        ),
        sa.CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
    )

    channel_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("notification_channels.id", ondelete="CASCADE"), index=True
    )
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    event: Mapped[RunEvent] = mapped_column(str_enum(RunEvent, name="event"))
    status: Mapped[DeliveryStatus] = mapped_column(
        str_enum(DeliveryStatus, name="status"), server_default=DeliveryStatus.PENDING.value
    )
    payload: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT)
    attempt_count: Mapped[int] = mapped_column(server_default=sa.text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(server_default=sa.func.now())
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    sent_at: Mapped[datetime | None]
