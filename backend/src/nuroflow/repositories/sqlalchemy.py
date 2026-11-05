"""Async SQLAlchemy implementations of the repository Protocols (API process)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from nuroflow.db.models import (
    ApiKey,
    DataRecord,
    RefreshToken,
    TaskAttempt,
    TaskLog,
    TaskRun,
    User,
    Workflow,
    WorkflowRun,
    WorkflowVersion,
)
from nuroflow.domain.enums import RunStatus, TaskStatus


class SqlAlchemyUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, user_id: uuid.UUID) -> User | None:
        return await self._s.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        # Matches the functional unique index uq_users_email_lower, so this is an index lookup.
        stmt = sa.select(User).where(sa.func.lower(User.email) == email.lower())
        return (await self._s.scalars(stmt)).one_or_none()

    async def add(self, user: User) -> None:
        self._s.add(user)
        await self._s.flush()

    async def list_page(self, *, limit: int, before_id: uuid.UUID | None = None) -> Sequence[User]:
        # Keyset pagination on UUIDv7: id order == creation order, so no extra index
        # is needed and page N costs the same as page 1 (unlike OFFSET).
        stmt = sa.select(User).order_by(User.id.desc()).limit(limit)
        if before_id is not None:
            stmt = stmt.where(User.id < before_id)
        return (await self._s.scalars(stmt)).all()


class SqlAlchemyRefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, token: RefreshToken) -> None:
        self._s.add(token)
        await self._s.flush()

    async def get_by_hash_for_update(self, token_hash: str) -> RefreshToken | None:
        stmt = (
            sa.select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
        )
        return (await self._s.scalars(stmt)).one_or_none()

    async def revoke_family(self, family_id: uuid.UUID, *, at: datetime) -> int:
        stmt = (
            sa.update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=at)
        )
        result = await self._s.execute(stmt)
        return int(result.rowcount)  # type: ignore[attr-defined]

    async def revoke_all_for_user(self, user_id: uuid.UUID, *, at: datetime) -> int:
        stmt = (
            sa.update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=at)
        )
        result = await self._s.execute(stmt)
        return int(result.rowcount)  # type: ignore[attr-defined]


class SqlAlchemyApiKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, api_key: ApiKey) -> None:
        self._s.add(api_key)
        await self._s.flush()

    async def get(self, key_id: uuid.UUID) -> ApiKey | None:
        return await self._s.get(ApiKey, key_id)

    async def get_by_prefix(self, prefix: str) -> ApiKey | None:
        stmt = sa.select(ApiKey).where(ApiKey.prefix == prefix)
        return (await self._s.scalars(stmt)).one_or_none()

    async def list_for_user(self, user_id: uuid.UUID) -> Sequence[ApiKey]:
        stmt = sa.select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.id.desc())
        return (await self._s.scalars(stmt)).all()


# --------------------------------------------------------------------------- #
# Workflows & runs (Phase 3)
# --------------------------------------------------------------------------- #
class SqlAlchemyWorkflowRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, workflow: Workflow) -> None:
        self._s.add(workflow)
        await self._s.flush()

    async def get(
        self, workflow_id: uuid.UUID, *, lock: bool = False, include_deleted: bool = False
    ) -> Workflow | None:
        stmt = sa.select(Workflow).where(Workflow.id == workflow_id)
        if not include_deleted:
            stmt = stmt.where(Workflow.deleted_at.is_(None))
        if lock:
            stmt = stmt.with_for_update()
        return (await self._s.scalars(stmt)).one_or_none()

    async def get_live_by_slug(self, owner_id: uuid.UUID, slug: str) -> Workflow | None:
        stmt = sa.select(Workflow).where(
            Workflow.owner_id == owner_id, Workflow.slug == slug, Workflow.deleted_at.is_(None)
        )
        return (await self._s.scalars(stmt)).one_or_none()

    async def list_page(
        self, *, owner_id: uuid.UUID | None, limit: int, before_id: uuid.UUID | None
    ) -> Sequence[Workflow]:
        stmt = (
            sa.select(Workflow)
            .where(Workflow.deleted_at.is_(None))
            .order_by(Workflow.id.desc())
            .limit(limit)
        )
        if owner_id is not None:
            stmt = stmt.where(Workflow.owner_id == owner_id)
        if before_id is not None:
            stmt = stmt.where(Workflow.id < before_id)
        return (await self._s.scalars(stmt)).all()

    async def add_version(self, version: WorkflowVersion) -> None:
        self._s.add(version)
        await self._s.flush()

    async def get_version(self, version_id: uuid.UUID) -> WorkflowVersion | None:
        return await self._s.get(WorkflowVersion, version_id)

    async def list_versions(self, workflow_id: uuid.UUID) -> Sequence[WorkflowVersion]:
        stmt = (
            sa.select(WorkflowVersion)
            .where(WorkflowVersion.workflow_id == workflow_id)
            .order_by(WorkflowVersion.version.desc())
        )
        return (await self._s.scalars(stmt)).all()

    async def max_version(self, workflow_id: uuid.UUID) -> int:
        stmt = sa.select(sa.func.coalesce(sa.func.max(WorkflowVersion.version), 0)).where(
            WorkflowVersion.workflow_id == workflow_id
        )
        return int((await self._s.execute(stmt)).scalar_one())


_OPEN_TASK_STATUSES = (TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.UP_FOR_RETRY)


class SqlAlchemyRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, run: WorkflowRun) -> None:
        self._s.add(run)
        await self._s.flush()

    async def get(self, run_id: uuid.UUID, *, lock: bool = False) -> WorkflowRun | None:
        stmt = sa.select(WorkflowRun).where(WorkflowRun.id == run_id)
        if lock:
            stmt = stmt.with_for_update()
        return (await self._s.scalars(stmt)).one_or_none()

    async def get_by_idempotency_key(self, key: str) -> WorkflowRun | None:
        stmt = sa.select(WorkflowRun).where(WorkflowRun.idempotency_key == key)
        return (await self._s.scalars(stmt)).one_or_none()

    async def count_active(self, workflow_id: uuid.UUID) -> int:
        # Served by the partial index ix_workflow_runs_active.
        stmt = sa.select(sa.func.count()).where(
            WorkflowRun.workflow_id == workflow_id,
            WorkflowRun.status.in_((RunStatus.PENDING, RunStatus.RUNNING)),
        )
        return int((await self._s.execute(stmt)).scalar_one())

    async def list_page(
        self,
        *,
        owner_id: uuid.UUID | None,
        workflow_id: uuid.UUID | None,
        status: RunStatus | None,
        limit: int,
        before_id: uuid.UUID | None,
    ) -> Sequence[WorkflowRun]:
        stmt = sa.select(WorkflowRun).order_by(WorkflowRun.id.desc()).limit(limit)
        if owner_id is not None:
            stmt = stmt.join(Workflow, Workflow.id == WorkflowRun.workflow_id).where(
                Workflow.owner_id == owner_id
            )
        if workflow_id is not None:
            stmt = stmt.where(WorkflowRun.workflow_id == workflow_id)
        if status is not None:
            stmt = stmt.where(WorkflowRun.status == status)
        if before_id is not None:
            stmt = stmt.where(WorkflowRun.id < before_id)
        return (await self._s.scalars(stmt)).all()

    async def task_runs(self, run_id: uuid.UUID) -> Sequence[TaskRun]:
        stmt = sa.select(TaskRun).where(TaskRun.workflow_run_id == run_id).order_by(TaskRun.id)
        return (await self._s.scalars(stmt)).all()

    async def get_task_run(self, task_run_id: uuid.UUID) -> TaskRun | None:
        return await self._s.get(TaskRun, task_run_id)

    async def attempts(self, task_run_id: uuid.UUID) -> Sequence[TaskAttempt]:
        stmt = (
            sa.select(TaskAttempt)
            .where(TaskAttempt.task_run_id == task_run_id)
            .order_by(TaskAttempt.attempt_number)
        )
        return (await self._s.scalars(stmt)).all()

    async def logs(self, task_run_id: uuid.UUID, *, after_id: int, limit: int) -> Sequence[TaskLog]:
        stmt = (
            sa.select(TaskLog)
            .where(TaskLog.task_run_id == task_run_id, TaskLog.id > after_id)
            .order_by(TaskLog.id)
            .limit(limit)
        )
        return (await self._s.scalars(stmt)).all()

    async def cancel_open_tasks(self, run_id: uuid.UUID, *, at: datetime) -> int:
        stmt = (
            sa.update(TaskRun)
            .where(TaskRun.workflow_run_id == run_id, TaskRun.status.in_(_OPEN_TASK_STATUSES))
            .values(status=TaskStatus.CANCELLED, finished_at=at, next_attempt_at=None)
        )
        result = await self._s.execute(stmt)
        return int(result.rowcount)  # type: ignore[attr-defined]

    async def records(
        self, *, owner_id: uuid.UUID, collection: str, limit: int, before_id: uuid.UUID | None
    ) -> Sequence[DataRecord]:
        stmt = (
            sa.select(DataRecord)
            .where(DataRecord.owner_id == owner_id, DataRecord.collection == collection)
            .order_by(DataRecord.id.desc())
            .limit(limit)
        )
        if before_id is not None:
            stmt = stmt.where(DataRecord.id < before_id)
        return (await self._s.scalars(stmt)).all()


class SqlAlchemyUnitOfWork:
    """Wraps one ``AsyncSession``. The session's lifetime is owned by the caller."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = SqlAlchemyUserRepository(session)
        self.refresh_tokens = SqlAlchemyRefreshTokenRepository(session)
        self.api_keys = SqlAlchemyApiKeyRepository(session)
        self.workflows = SqlAlchemyWorkflowRepository(session)
        self.runs = SqlAlchemyRunRepository(session)

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
