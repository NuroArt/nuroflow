"""Reusable column mixins.

They are kept as mixins rather than a base-class hierarchy so each model declares
exactly the columns it has. Append-only tables (attempts, logs, versions) get
``CreatedAtMixin`` only. An ``updated_at`` on immutable rows would be a lie.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from nuroflow.core.ids import uuid7


class UUIDPrimaryKeyMixin:
    """UUIDv7 primary key generated in Python, so IDs are known before flush."""

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7, sort_order=-100)


class CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(
        server_default=sa.func.now(), nullable=False, sort_order=100
    )


class TimestampMixin(CreatedAtMixin):
    updated_at: Mapped[datetime] = mapped_column(
        server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False, sort_order=101
    )
