"""scheduler and admission

* ``workflow_runs.admitted_at``: backpressure. Automated runs beyond max_active_runs
  wait (admitted_at IS NULL) instead of being rejected. Existing runs are backfilled
  as admitted at creation, which matches their Phase 3 behaviour.
* ``workflow_triggers.spec_key``: trigger identity across versions. Expand, backfill,
  contract: add nullable, backfill existing rows, then SET NOT NULL.
* Partial indexes for the scheduler: waiting runs, active deadlines, queued tasks.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-05 21:47:15.561034+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_task_runs_queued_since",
        "task_runs",
        ["queued_at"],
        unique=False,
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.add_column(
        "workflow_runs", sa.Column("admitted_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.execute("UPDATE workflow_runs SET admitted_at = created_at")
    op.drop_index(
        op.f("ix_workflow_runs_deadline_running"),
        table_name="workflow_runs",
        postgresql_where="(((status)::text = 'running'::text) AND (deadline_at IS NOT NULL))",
    )
    op.create_index(
        "ix_workflow_runs_deadline_active",
        "workflow_runs",
        ["deadline_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('pending', 'running') AND deadline_at IS NOT NULL"),
    )
    op.create_index(
        "ix_workflow_runs_waiting",
        "workflow_runs",
        ["workflow_id", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'pending' AND admitted_at IS NULL"),
    )
    op.add_column("workflow_triggers", sa.Column("spec_key", sa.String(length=300), nullable=True))
    # Pre-Phase-4 triggers were never synced from specs; give them unique legacy keys.
    # The next version save replaces them with real keys.
    op.execute("UPDATE workflow_triggers SET spec_key = type || ':legacy:' || id::text")
    op.alter_column("workflow_triggers", "spec_key", nullable=False)
    op.create_unique_constraint(
        op.f("uq_workflow_triggers_workflow_id_spec_key"),
        "workflow_triggers",
        ["workflow_id", "spec_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("uq_workflow_triggers_workflow_id_spec_key"), "workflow_triggers", type_="unique"
    )
    op.drop_column("workflow_triggers", "spec_key")
    op.drop_index(
        "ix_workflow_runs_waiting",
        table_name="workflow_runs",
        postgresql_where=sa.text("status = 'pending' AND admitted_at IS NULL"),
    )
    op.drop_index(
        "ix_workflow_runs_deadline_active",
        table_name="workflow_runs",
        postgresql_where=sa.text("status IN ('pending', 'running') AND deadline_at IS NOT NULL"),
    )
    op.create_index(
        op.f("ix_workflow_runs_deadline_running"),
        "workflow_runs",
        ["deadline_at"],
        unique=False,
        postgresql_where="(((status)::text = 'running'::text) AND (deadline_at IS NOT NULL))",
    )
    op.drop_column("workflow_runs", "admitted_at")
    op.drop_index(
        "ix_task_runs_queued_since",
        table_name="task_runs",
        postgresql_where=sa.text("status = 'queued'"),
    )
