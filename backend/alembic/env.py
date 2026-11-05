"""Alembic migration environment.

* The URL comes from NuroFlow settings, so there's one source of truth for configuration.
* ``compare_type`` and ``compare_server_default`` make autogenerate catch column type
  and default drift, not just missing columns.
* Every migration runs in a single transaction (Postgres has transactional DDL), so a
  failed migration leaves no half-applied schema.
* A session-level lock timeout stops a migration from queueing behind a long query
  and blocking all traffic while it waits.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from nuroflow.core.config import get_settings
from nuroflow.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# A URL set programmatically (e.g. by the test suite) takes precedence over settings.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", get_settings().sqlalchemy_url.replace("%", "%%"))

target_metadata = Base.metadata

LOCK_TIMEOUT = "5s"


def run_migrations_offline() -> None:
    """Emit SQL to stdout (``alembic upgrade head --sql``) for DBA review."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        connection.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
        # SQLAlchemy 2.0 autobegins a transaction on the first execute. Commit it, or
        # Alembic joins that outer transaction and its own commits become no-ops,
        # which silently rolls back every migration when the connection closes.
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
