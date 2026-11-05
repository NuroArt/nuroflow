"""initial schema

Creates the complete Phase 1 schema: identity (users, refresh_tokens, api_keys),
credentials (connections), definitions (workflows, workflow_versions,
workflow_triggers), ingestion (events), execution (workflow_runs, task_runs,
task_attempts, task_logs) and notifications (channels, subscriptions, deliveries).

Generated with ``alembic revision --autogenerate`` and then reviewed by hand. The only
manual change is the circular workflows <-> workflow_versions foreign key, which is
created after both tables exist and dropped first on downgrade.

Revision ID: 0001
Revises:
Create Date: 2026-11-04 23:49:31.425283+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=True),
        sa.Column(
            "role",
            sa.Enum(
                "admin",
                "editor",
                "viewer",
                name="role",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="editor",
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_login_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(
        "uq_users_email_lower", "users", [sa.literal_column("lower(email)")], unique=True
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_api_keys_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("prefix", name=op.f("uq_api_keys_prefix")),
    )
    op.create_index(op.f("ix_api_keys_user_id"), "api_keys", ["user_id"], unique=False)
    op.create_table(
        "connections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "telegram",
                "openai",
                "anthropic",
                "http",
                name="type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("encrypted_secret", sa.LargeBinary(), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_connections_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connections")),
        sa.UniqueConstraint("owner_id", "name", name=op.f("uq_connections_owner_id_name")),
    )
    op.create_index(op.f("ix_connections_owner_id"), "connections", ["owner_id"], unique=False)
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("family_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("replaced_by_id", sa.UUID(), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by_id"],
            ["refresh_tokens.id"],
            name=op.f("fk_refresh_tokens_replaced_by_id_refresh_tokens"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_refresh_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_refresh_tokens_token_hash")),
    )
    op.create_index(
        op.f("ix_refresh_tokens_family_id"), "refresh_tokens", ["family_id"], unique=False
    )
    op.create_index(op.f("ix_refresh_tokens_user_id"), "refresh_tokens", ["user_id"], unique=False)
    op.create_table(
        "workflows",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("is_paused", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("max_active_runs", sa.Integer(), server_default=sa.text("16"), nullable=False),
        sa.Column("current_version_id", sa.UUID(), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9][a-z0-9-]{0,99}$'", name=op.f("ck_workflows_slug_format")
        ),
        sa.CheckConstraint(
            "max_active_runs > 0", name=op.f("ck_workflows_max_active_runs_positive")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_workflows_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflows")),
    )
    op.create_index(op.f("ix_workflows_owner_id"), "workflows", ["owner_id"], unique=False)
    op.create_index(
        "uq_workflows_owner_id_slug_live",
        "workflows",
        ["owner_id", "slug"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("type", sa.String(length=200), nullable=False),
        sa.Column("source", sa.String(length=200), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("api_key_id", sa.UUID(), nullable=True),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["api_keys.id"],
            name=op.f("fk_events_api_key_id_api_keys"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_events_owner_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint(
            "owner_id", "idempotency_key", name=op.f("uq_events_owner_id_idempotency_key")
        ),
    )
    op.create_index(
        "ix_events_owner_id_type_received_at",
        "events",
        ["owner_id", "type", "received_at"],
        unique=False,
    )
    op.create_table(
        "notification_channels",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "telegram",
                "webhook",
                name="type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("connection_id", sa.UUID(), nullable=True),
        sa.Column("target", sa.String(length=500), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_notification_channels_connection_id_connections"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_notification_channels_owner_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_channels")),
        sa.UniqueConstraint(
            "owner_id", "name", name=op.f("uq_notification_channels_owner_id_name")
        ),
    )
    op.create_index(
        op.f("ix_notification_channels_connection_id"),
        "notification_channels",
        ["connection_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_channels_owner_id"),
        "notification_channels",
        ["owner_id"],
        unique=False,
    )
    op.create_table(
        "workflow_triggers",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workflow_id", sa.UUID(), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "cron",
                "event",
                "webhook",
                name="type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=100), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("cron_expression", sa.String(length=100), nullable=True),
        sa.Column("timezone", sa.String(length=64), server_default="UTC", nullable=False),
        sa.Column("catchup", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("next_fire_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_fired_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("event_type", sa.String(length=200), nullable=True),
        sa.Column(
            "event_filter",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("webhook_secret_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "type <> 'cron' OR cron_expression IS NOT NULL",
            name=op.f("ck_workflow_triggers_cron_requires_expression"),
        ),
        sa.CheckConstraint(
            "type <> 'event' OR event_type IS NOT NULL",
            name=op.f("ck_workflow_triggers_event_requires_event_type"),
        ),
        sa.CheckConstraint(
            "type <> 'webhook' OR webhook_secret_encrypted IS NOT NULL",
            name=op.f("ck_workflow_triggers_webhook_requires_secret"),
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name=op.f("fk_workflow_triggers_workflow_id_workflows"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_triggers")),
    )
    op.create_index(
        "ix_workflow_triggers_due_cron",
        "workflow_triggers",
        ["next_fire_at"],
        unique=False,
        postgresql_where=sa.text("is_enabled AND type = 'cron'"),
    )
    op.create_index(
        "ix_workflow_triggers_event_type_enabled",
        "workflow_triggers",
        ["event_type"],
        unique=False,
        postgresql_where=sa.text("is_enabled AND type = 'event'"),
    )
    op.create_index(
        op.f("ix_workflow_triggers_workflow_id"), "workflow_triggers", ["workflow_id"], unique=False
    )
    op.create_table(
        "workflow_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workflow_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "definition", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=False
        ),
        sa.Column("checksum", sa.CHAR(length=64), nullable=False),
        sa.Column("change_note", sa.Text(), nullable=True),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_workflow_versions_version_positive")),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_workflow_versions_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name=op.f("fk_workflow_versions_workflow_id_workflows"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_versions")),
        sa.UniqueConstraint(
            "workflow_id", "version", name=op.f("uq_workflow_versions_workflow_id_version")
        ),
    )
    # Circular reference workflows.current_version_id -> workflow_versions.id can only be
    # added once both tables exist (the model declares it with use_alter=True).
    op.create_foreign_key(
        "fk_workflows_current_version_id_workflow_versions",
        "workflows",
        "workflow_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_table(
        "notification_subscriptions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workflow_id", sa.UUID(), nullable=False),
        sa.Column("channel_id", sa.UUID(), nullable=False),
        sa.Column("events", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "events <@ ARRAY['run.started', 'run.succeeded', 'run.failed', 'task.retrying']::text[]",
            name=op.f("ck_notification_subscriptions_events_valid"),
        ),
        sa.CheckConstraint(
            "cardinality(events) > 0", name=op.f("ck_notification_subscriptions_events_not_empty")
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["notification_channels.id"],
            name=op.f("fk_notification_subscriptions_channel_id_notification_channels"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name=op.f("fk_notification_subscriptions_workflow_id_workflows"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_subscriptions")),
        sa.UniqueConstraint(
            "workflow_id",
            "channel_id",
            name=op.f("uq_notification_subscriptions_workflow_id_channel_id"),
        ),
    )
    op.create_index(
        op.f("ix_notification_subscriptions_channel_id"),
        "notification_subscriptions",
        ["channel_id"],
        unique=False,
    )
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workflow_id", sa.UUID(), nullable=False),
        sa.Column("workflow_version_id", sa.UUID(), nullable=False),
        sa.Column(
            "trigger_type",
            sa.Enum(
                "manual",
                "cron",
                "event",
                "webhook",
                name="trigger_type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("trigger_id", sa.UUID(), nullable=True),
        sa.Column("event_id", sa.UUID(), nullable=True),
        sa.Column("triggered_by_id", sa.UUID(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "params",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "trigger_payload",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("scheduled_for", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_workflow_runs_finished_after_started"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("fk_workflow_runs_event_id_events"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_id"],
            ["workflow_triggers.id"],
            name=op.f("fk_workflow_runs_trigger_id_workflow_triggers"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["triggered_by_id"],
            ["users.id"],
            name=op.f("fk_workflow_runs_triggered_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name=op.f("fk_workflow_runs_workflow_id_workflows"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"],
            ["workflow_versions.id"],
            name=op.f("fk_workflow_runs_workflow_version_id_workflow_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_runs")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_workflow_runs_idempotency_key")),
    )
    op.create_index(
        "ix_workflow_runs_active",
        "workflow_runs",
        ["workflow_id", "status"],
        unique=False,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.create_index(
        "ix_workflow_runs_deadline_running",
        "workflow_runs",
        ["deadline_at"],
        unique=False,
        postgresql_where=sa.text("status = 'running' AND deadline_at IS NOT NULL"),
    )
    op.create_index(op.f("ix_workflow_runs_event_id"), "workflow_runs", ["event_id"], unique=False)
    op.create_index(
        op.f("ix_workflow_runs_trigger_id"), "workflow_runs", ["trigger_id"], unique=False
    )
    op.create_index(
        "ix_workflow_runs_workflow_id_created_at",
        "workflow_runs",
        ["workflow_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(
        op.f("ix_workflow_runs_workflow_version_id"),
        "workflow_runs",
        ["workflow_version_id"],
        unique=False,
    )
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("channel_id", sa.UUID(), nullable=False),
        sa.Column("workflow_run_id", sa.UUID(), nullable=True),
        sa.Column(
            "event",
            sa.Enum(
                "run.started",
                "run.succeeded",
                "run.failed",
                "task.retrying",
                name="event",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "sent",
                "failed",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_notification_deliveries_attempt_count_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["notification_channels.id"],
            name=op.f("fk_notification_deliveries_channel_id_notification_channels"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_notification_deliveries_workflow_run_id_workflow_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_deliveries")),
    )
    op.create_index(
        op.f("ix_notification_deliveries_channel_id"),
        "notification_deliveries",
        ["channel_id"],
        unique=False,
    )
    op.create_index(
        "ix_notification_deliveries_pending_due",
        "notification_deliveries",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        op.f("ix_notification_deliveries_workflow_run_id"),
        "notification_deliveries",
        ["workflow_run_id"],
        unique=False,
    )
    op.create_table(
        "task_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workflow_run_id", sa.UUID(), nullable=False),
        sa.Column("task_key", sa.String(length=100), nullable=False),
        sa.Column("operator", sa.String(length=100), nullable=False),
        sa.Column("queue", sa.String(length=50), server_default="default", nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "queued",
                "running",
                "up_for_retry",
                "succeeded",
                "failed",
                "skipped",
                "cancelled",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "input", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "output", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("queued_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(length=200), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_count <= max_attempts", name=op.f("ck_task_runs_attempts_within_limit")
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name=op.f("ck_task_runs_attempt_count_non_negative")
        ),
        sa.CheckConstraint("max_attempts >= 1", name=op.f("ck_task_runs_max_attempts_positive")),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_task_runs_workflow_run_id_workflow_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_runs")),
        sa.UniqueConstraint(
            "workflow_run_id", "task_key", name=op.f("uq_task_runs_workflow_run_id_task_key")
        ),
    )
    op.create_index(
        "ix_task_runs_in_flight_heartbeat",
        "task_runs",
        ["heartbeat_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_task_runs_retry_due",
        "task_runs",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("status = 'up_for_retry'"),
    )
    op.create_table(
        "task_attempts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("task_run_id", sa.UUID(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "failed",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="running",
            nullable=False,
        ),
        sa.Column("worker_id", sa.String(length=200), nullable=True),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_type", sa.String(length=200), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("traceback", sa.Text(), nullable=True),
        sa.Column(
            "metrics",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_number >= 1", name=op.f("ck_task_attempts_attempt_number_positive")
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name=op.f("ck_task_attempts_duration_non_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["task_run_id"],
            ["task_runs.id"],
            name=op.f("fk_task_attempts_task_run_id_task_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_attempts")),
        sa.UniqueConstraint(
            "task_run_id",
            "attempt_number",
            name=op.f("uq_task_attempts_task_run_id_attempt_number"),
        ),
    )
    op.create_table(
        "task_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("task_run_id", sa.UUID(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "level",
            sa.Enum(
                "debug",
                "info",
                "warning",
                "error",
                name="level",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="info",
            nullable=False,
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "context",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["task_run_id"],
            ["task_runs.id"],
            name=op.f("fk_task_logs_task_run_id_task_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_logs")),
    )
    op.create_index("ix_task_logs_task_run_id_id", "task_logs", ["task_run_id", "id"], unique=False)


def downgrade() -> None:
    op.drop_constraint(
        "fk_workflows_current_version_id_workflow_versions", "workflows", type_="foreignkey"
    )
    op.drop_index("ix_task_logs_task_run_id_id", table_name="task_logs")
    op.drop_table("task_logs")
    op.drop_table("task_attempts")
    op.drop_index(
        "ix_task_runs_retry_due",
        table_name="task_runs",
        postgresql_where=sa.text("status = 'up_for_retry'"),
    )
    op.drop_index(
        "ix_task_runs_in_flight_heartbeat",
        table_name="task_runs",
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.drop_table("task_runs")
    op.drop_index(
        op.f("ix_notification_deliveries_workflow_run_id"), table_name="notification_deliveries"
    )
    op.drop_index(
        "ix_notification_deliveries_pending_due",
        table_name="notification_deliveries",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index(
        op.f("ix_notification_deliveries_channel_id"), table_name="notification_deliveries"
    )
    op.drop_table("notification_deliveries")
    op.drop_index(op.f("ix_workflow_runs_workflow_version_id"), table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_workflow_id_created_at", table_name="workflow_runs")
    op.drop_index(op.f("ix_workflow_runs_trigger_id"), table_name="workflow_runs")
    op.drop_index(op.f("ix_workflow_runs_event_id"), table_name="workflow_runs")
    op.drop_index(
        "ix_workflow_runs_deadline_running",
        table_name="workflow_runs",
        postgresql_where=sa.text("status = 'running' AND deadline_at IS NOT NULL"),
    )
    op.drop_index(
        "ix_workflow_runs_active",
        table_name="workflow_runs",
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.drop_table("workflow_runs")
    op.drop_index(
        op.f("ix_notification_subscriptions_channel_id"), table_name="notification_subscriptions"
    )
    op.drop_table("notification_subscriptions")
    op.drop_table("workflow_versions")
    op.drop_index(op.f("ix_workflow_triggers_workflow_id"), table_name="workflow_triggers")
    op.drop_index(
        "ix_workflow_triggers_event_type_enabled",
        table_name="workflow_triggers",
        postgresql_where=sa.text("is_enabled AND type = 'event'"),
    )
    op.drop_index(
        "ix_workflow_triggers_due_cron",
        table_name="workflow_triggers",
        postgresql_where=sa.text("is_enabled AND type = 'cron'"),
    )
    op.drop_table("workflow_triggers")
    op.drop_index(op.f("ix_notification_channels_owner_id"), table_name="notification_channels")
    op.drop_index(
        op.f("ix_notification_channels_connection_id"), table_name="notification_channels"
    )
    op.drop_table("notification_channels")
    op.drop_index("ix_events_owner_id_type_received_at", table_name="events")
    op.drop_table("events")
    op.drop_index(
        "uq_workflows_owner_id_slug_live",
        table_name="workflows",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_index(op.f("ix_workflows_owner_id"), table_name="workflows")
    op.drop_table("workflows")
    op.drop_index(op.f("ix_refresh_tokens_user_id"), table_name="refresh_tokens")
    op.drop_index(op.f("ix_refresh_tokens_family_id"), table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index(op.f("ix_connections_owner_id"), table_name="connections")
    op.drop_table("connections")
    op.drop_index(op.f("ix_api_keys_user_id"), table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index("uq_users_email_lower", table_name="users")
    op.drop_table("users")
