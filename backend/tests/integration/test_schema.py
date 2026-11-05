"""Migrations and schema rules, verified against a real PostgreSQL."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from nuroflow.db.models import (
    Base,
    NotificationChannel,
    NotificationSubscription,
    TaskRun,
    User,
    Workflow,
    WorkflowRun,
    WorkflowTrigger,
    WorkflowVersion,
)
from nuroflow.domain.enums import (
    ChannelType,
    RunStatus,
    RunTriggerType,
    TaskStatus,
    TriggerType,
)

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# Migrations
# --------------------------------------------------------------------------- #
def test_migrations_match_models(engine: Engine, migrated_db: None) -> None:
    """Fail when someone edits a model and forgets to write the migration."""
    with engine.connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "compare_server_default": True}
        )
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], f"models and migrations have drifted: {diff}"


def test_downgrade_then_upgrade_round_trips(alembic_config: Config, engine: Engine) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")
    with engine.connect() as conn:
        remaining = set(sa.inspect(conn).get_table_names()) - {"alembic_version"}
    assert remaining == set()
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")


# --------------------------------------------------------------------------- #
# Constraint behaviour
# --------------------------------------------------------------------------- #
@pytest.fixture
def make_user(session: Session) -> Callable[..., User]:
    def _make(email: str = "jon@nuroworks.com") -> User:
        user = User(email=email, hashed_password="argon2-hash")
        session.add(user)
        session.flush()
        return user

    return _make


@pytest.fixture
def workflow(session: Session, make_user: Callable[..., User]) -> Workflow:
    wf = Workflow(owner_id=make_user().id, slug="lead-qualification", name="Lead Qualification")
    session.add(wf)
    session.flush()
    return wf


def _assert_rejected(session: Session, obj: object, constraint: str) -> None:
    session.add(obj)
    with pytest.raises(sa.exc.IntegrityError, match=constraint):
        session.flush()
    session.rollback()


def test_server_defaults_and_uuid7_keys(session: Session, make_user: Callable[..., User]) -> None:
    user = make_user()
    session.refresh(user)
    assert user.id.version == 7
    assert user.role == "editor"
    assert user.is_active is True
    assert user.created_at.tzinfo is not None


def test_email_unique_case_insensitive(session: Session, make_user: Callable[..., User]) -> None:
    make_user("Jon@NuroWorks.com")
    _assert_rejected(
        session, User(email="jon@nuroworks.com", hashed_password="x"), "uq_users_email_lower"
    )


def test_invalid_enum_value_rejected_by_database(session: Session) -> None:
    """Raw SQL bypasses Python validation, so the CHECK constraint is the last line of defence."""
    with pytest.raises(sa.exc.IntegrityError, match="ck_users_role"):
        session.execute(
            sa.text(
                "INSERT INTO users (id, email, hashed_password, role) "
                "VALUES (gen_random_uuid(), 'x@y.z', 'h', 'superuser')"
            )
        )
    session.rollback()


def test_workflow_slug_format_enforced(session: Session, make_user: Callable[..., User]) -> None:
    owner = make_user()
    _assert_rejected(
        session, Workflow(owner_id=owner.id, slug="Bad Slug!", name="x"), "ck_workflows_slug_format"
    )


def test_soft_deleted_workflow_frees_its_slug(session: Session, workflow: Workflow) -> None:
    workflow.deleted_at = datetime.now(UTC)
    session.flush()
    session.add(Workflow(owner_id=workflow.owner_id, slug=workflow.slug, name="v2"))
    session.flush()  # no IntegrityError: the partial unique index ignores deleted rows


def test_live_duplicate_slug_rejected(session: Session, workflow: Workflow) -> None:
    _assert_rejected(
        session,
        Workflow(owner_id=workflow.owner_id, slug=workflow.slug, name="dup"),
        "uq_workflows_owner_id_slug_live",
    )


def test_cron_trigger_requires_expression(session: Session, workflow: Workflow) -> None:
    _assert_rejected(
        session,
        WorkflowTrigger(workflow_id=workflow.id, type=TriggerType.CRON, spec_key="cron:x"),
        "cron_requires_expression",
    )


def test_subscription_events_must_be_known(session: Session, workflow: Workflow) -> None:
    channel = NotificationChannel(
        owner_id=workflow.owner_id, name="ops", type=ChannelType.TELEGRAM, target="123"
    )
    session.add(channel)
    session.flush()
    _assert_rejected(
        session,
        NotificationSubscription(
            workflow_id=workflow.id, channel_id=channel.id, events=["run.boom"]
        ),
        "events_valid",
    )


def test_run_graph_round_trips_and_task_keys_are_unique(
    session: Session, workflow: Workflow
) -> None:
    version = WorkflowVersion(
        workflow_id=workflow.id, version=1, definition={"tasks": []}, checksum="0" * 64
    )
    session.add(version)
    session.flush()
    workflow.current_version_id = version.id

    run = WorkflowRun(
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        trigger_type=RunTriggerType.MANUAL,
        idempotency_key="manual:test-1",
    )
    run.task_runs = [
        TaskRun(task_key="qualify", operator="ai.structured", max_attempts=4),
        TaskRun(task_key="alert", operator="telegram.send"),
    ]
    session.add(run)
    session.flush()
    session.refresh(run)
    assert run.status is RunStatus.PENDING

    statuses = session.scalars(
        sa.select(TaskRun.status).where(TaskRun.workflow_run_id == run.id)
    ).all()
    assert statuses == [TaskStatus.PENDING, TaskStatus.PENDING]

    _assert_rejected(
        session,
        TaskRun(workflow_run_id=run.id, task_key="qualify", operator="noop"),
        "uq_task_runs_workflow_run_id",
    )


def test_attempts_cannot_exceed_max_attempts(session: Session, workflow: Workflow) -> None:
    version = WorkflowVersion(workflow_id=workflow.id, version=1, definition={}, checksum="0" * 64)
    session.add(version)
    session.flush()
    run = WorkflowRun(
        workflow_id=workflow.id, workflow_version_id=version.id, trigger_type=RunTriggerType.MANUAL
    )
    session.add(run)
    session.flush()
    _assert_rejected(
        session,
        TaskRun(
            workflow_run_id=run.id, task_key="t", operator="noop", attempt_count=3, max_attempts=2
        ),
        "attempts_within_limit",
    )


def test_run_idempotency_key_prevents_duplicate_runs(session: Session, workflow: Workflow) -> None:
    version = WorkflowVersion(workflow_id=workflow.id, version=1, definition={}, checksum="0" * 64)
    session.add(version)
    session.flush()

    def run() -> WorkflowRun:
        return WorkflowRun(
            workflow_id=workflow.id,
            workflow_version_id=version.id,
            trigger_type=RunTriggerType.CRON,
            idempotency_key="cron:trigger-1:2026-11-04T09:00:00Z",
        )

    session.add(run())
    session.flush()
    _assert_rejected(session, run(), "uq_workflow_runs_idempotency_key")
