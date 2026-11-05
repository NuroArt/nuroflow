"""Schema guardrails that need no database: they run on every commit in milliseconds."""

from __future__ import annotations

import sqlalchemy as sa

from nuroflow.db.models import Base

EXPECTED_TABLES = {
    "users",
    "refresh_tokens",
    "api_keys",
    "connections",
    "workflows",
    "workflow_versions",
    "workflow_triggers",
    "events",
    "workflow_runs",
    "task_runs",
    "task_attempts",
    "task_logs",
    "notification_channels",
    "notification_subscriptions",
    "notification_deliveries",
}

# Foreign keys that intentionally have no supporting index. Postgres doesn't index FKs
# automatically, and an unindexed FK makes deletes on the parent table do a sequential
# scan. Each exception needs a reason.
UNINDEXED_FK_ALLOWLIST = {
    # Users are deactivated, never hard-deleted, so these SET NULL scans never run hot.
    ("workflow_versions", "created_by_id"),
    ("workflow_runs", "triggered_by_id"),
    ("refresh_tokens", "replaced_by_id"),  # rows pruned by expiry, not by parent delete
    ("events", "api_key_id"),  # API keys are revoked (soft), not deleted
    # Versions are immutable and only deleted via workflow cascade.
    ("workflows", "current_version_id"),
}


def test_all_expected_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_every_constraint_and_index_has_an_explicit_name() -> None:
    unnamed = [
        f"{table.name}: {type(item).__name__}"
        for table in Base.metadata.tables.values()
        for item in [*table.constraints, *table.indexes]
        if item.name is None
    ]
    assert not unnamed, f"constraints without deterministic names: {unnamed}"


def test_every_timestamp_column_is_timezone_aware() -> None:
    naive = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, sa.DateTime) and not column.type.timezone
    ]
    assert not naive, f"naive timestamp columns: {naive}"


def _indexed_leading_columns(table: sa.Table) -> set[str]:
    leading: set[str] = set()
    for index in table.indexes:
        cols = list(index.columns)
        if cols:
            leading.add(cols[0].name)
    for constraint in table.constraints:
        if isinstance(constraint, sa.PrimaryKeyConstraint | sa.UniqueConstraint):
            cols = list(constraint.columns)
            if cols:
                leading.add(cols[0].name)
    return leading


def test_every_foreign_key_is_indexed_or_explicitly_allowed() -> None:
    missing = []
    for table in Base.metadata.tables.values():
        leading = _indexed_leading_columns(table)
        for fk in table.foreign_keys:
            key = (table.name, fk.parent.name)
            if fk.parent.name not in leading and key not in UNINDEXED_FK_ALLOWLIST:
                missing.append(".".join(key))
    assert not missing, f"unindexed foreign keys: {sorted(missing)}"


def test_allowlist_has_no_stale_entries() -> None:
    fk_columns = {
        (table.name, fk.parent.name)
        for table in Base.metadata.tables.values()
        for fk in table.foreign_keys
    }
    assert fk_columns >= UNINDEXED_FK_ALLOWLIST
