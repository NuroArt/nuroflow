"""Records written by the ``data.store`` operator ("Store in Database" in the lead workflow)."""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from nuroflow.db.base import Base
from nuroflow.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class DataRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A JSON document in a named collection, owned by the workflow's owner.

    ``task_run_id`` is unique: a retried ``data.store`` task *replaces* its own record
    (``INSERT … ON CONFLICT (task_run_id) DO UPDATE``) instead of duplicating it.
    """

    __tablename__ = "data_records"
    __table_args__ = (
        sa.Index("ix_data_records_owner_id_collection_id", "owner_id", "collection", "id"),
        sa.CheckConstraint("collection ~ '^[a-z][a-z0-9_-]{0,99}$'", name="collection_format"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"))
    collection: Mapped[str] = mapped_column(sa.String(100))
    record: Mapped[dict[str, Any]]
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("workflow_runs.id", ondelete="SET NULL"), index=True
    )
    task_run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("task_runs.id", ondelete="SET NULL"), unique=True
    )
