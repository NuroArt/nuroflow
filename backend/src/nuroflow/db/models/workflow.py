"""Workflow definitions: workflows, immutable versions, and triggers."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nuroflow.db.base import EMPTY_TEXT_ARRAY, Base, str_enum
from nuroflow.db.mixins import CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from nuroflow.domain.enums import TriggerType


class Workflow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Mutable workflow metadata. The DAG itself lives in :class:`WorkflowVersion`."""

    __tablename__ = "workflows"
    __table_args__ = (
        # Slugs are unique per owner among *live* workflows, so a soft-deleted
        # workflow frees its slug for reuse.
        sa.Index(
            "uq_workflows_owner_id_slug_live",
            "owner_id",
            "slug",
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        ),
        sa.CheckConstraint("max_active_runs > 0", name="max_active_runs_positive"),
        sa.CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{0,99}$'", name="slug_format"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    slug: Mapped[str] = mapped_column(sa.String(100))
    name: Mapped[str] = mapped_column(sa.String(200))
    description: Mapped[str | None] = mapped_column(sa.Text)
    tags: Mapped[list[str]] = mapped_column(server_default=EMPTY_TEXT_ARRAY)
    is_paused: Mapped[bool] = mapped_column(server_default=sa.false())
    max_active_runs: Mapped[int] = mapped_column(server_default=sa.text("16"))
    # Circular FK (workflow -> version -> workflow): use_alter creates it after both
    # tables exist, so migrations work in either order.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey(
            "workflow_versions.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_workflows_current_version_id_workflow_versions",
        )
    )
    deleted_at: Mapped[datetime | None]

    versions: Mapped[list[WorkflowVersion]] = relationship(
        back_populates="workflow",
        foreign_keys="WorkflowVersion.workflow_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="WorkflowVersion.version",
        lazy="raise",
    )
    current_version: Mapped[WorkflowVersion | None] = relationship(
        foreign_keys=[current_version_id], post_update=True, lazy="raise"
    )
    triggers: Mapped[list[WorkflowTrigger]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<Workflow id={self.id} slug={self.slug!r}>"


class WorkflowVersion(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Immutable snapshot of a workflow definition. Runs pin a version.

    ``definition`` is the validated ``WorkflowSpec`` (Phase 3) as JSON. ``checksum`` is
    the SHA-256 of its canonical JSON form, used to skip no-op saves and to verify
    integrity.
    """

    __tablename__ = "workflow_versions"
    __table_args__ = (
        sa.UniqueConstraint("workflow_id", "version"),
        sa.CheckConstraint("version > 0", name="version_positive"),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workflows.id", ondelete="CASCADE")
    )
    version: Mapped[int]
    definition: Mapped[dict[str, Any]]
    checksum: Mapped[str] = mapped_column(sa.CHAR(64))
    change_note: Mapped[str | None] = mapped_column(sa.Text)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("users.id", ondelete="SET NULL")
    )

    workflow: Mapped[Workflow] = relationship(
        back_populates="versions", foreign_keys=[workflow_id], lazy="raise"
    )


class WorkflowTrigger(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A persistent trigger: cron schedule, event subscription, or signed webhook."""

    __tablename__ = "workflow_triggers"
    __table_args__ = (
        sa.CheckConstraint(
            "type <> 'cron' OR cron_expression IS NOT NULL", name="cron_requires_expression"
        ),
        sa.CheckConstraint(
            "type <> 'event' OR event_type IS NOT NULL", name="event_requires_event_type"
        ),
        sa.CheckConstraint(
            "type <> 'webhook' OR webhook_secret_encrypted IS NOT NULL",
            name="webhook_requires_secret",
        ),
        # Scheduler hot path: "which enabled cron triggers are due?"
        sa.Index(
            "ix_workflow_triggers_due_cron",
            "next_fire_at",
            postgresql_where=sa.text("is_enabled AND type = 'cron'"),
        ),
        # Event fan-out hot path: "which enabled triggers listen to this event type?"
        sa.Index(
            "ix_workflow_triggers_event_type_enabled",
            "event_type",
            postgresql_where=sa.text("is_enabled AND type = 'event'"),
        ),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("workflows.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[TriggerType] = mapped_column(str_enum(TriggerType, name="type"))
    name: Mapped[str | None] = mapped_column(sa.String(100))
    is_enabled: Mapped[bool] = mapped_column(server_default=sa.true())

    # cron
    cron_expression: Mapped[str | None] = mapped_column(sa.String(100))
    timezone: Mapped[str] = mapped_column(sa.String(64), server_default="UTC")
    catchup: Mapped[bool] = mapped_column(server_default=sa.false())
    next_fire_at: Mapped[datetime | None]
    last_fired_at: Mapped[datetime | None]

    # event
    event_type: Mapped[str | None] = mapped_column(sa.String(200))
    event_filter: Mapped[dict[str, Any] | None]  # JSON containment filter on payload (@>)

    # webhook: HMAC verification needs the secret itself, so it's encrypted, not hashed
    webhook_secret_encrypted: Mapped[bytes | None] = mapped_column(sa.LargeBinary)

    workflow: Mapped[Workflow] = relationship(back_populates="triggers", lazy="raise")
