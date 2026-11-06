"""Async SQLAlchemy implementations of the repository Protocols (API process)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from nuroflow.db.models import (
    ApiKey,
    Connection,
    DataRecord,
    Event,
    NotificationChannel,
    NotificationDelivery,
    NotificationSubscription,
    RefreshToken,
    TaskAttempt,
    TaskLog,
    TaskRun,
    User,
    Workflow,
    WorkflowRun,
    WorkflowTrigger,
    WorkflowVersion,
)
from nuroflow.domain.enums import RunStatus, TaskStatus, TriggerType


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

    async def list_triggers(self, workflow_id: uuid.UUID) -> Sequence[WorkflowTrigger]:
        stmt = (
            sa.select(WorkflowTrigger)
            .where(WorkflowTrigger.workflow_id == workflow_id)
            .order_by(WorkflowTrigger.id)
        )
        return (await self._s.scalars(stmt)).all()

    async def add_trigger(self, trigger: WorkflowTrigger) -> None:
        self._s.add(trigger)
        await self._s.flush()

    async def delete_trigger(self, trigger: WorkflowTrigger) -> None:
        await self._s.delete(trigger)
        await self._s.flush()

    async def get_trigger(self, trigger_id: uuid.UUID) -> WorkflowTrigger | None:
        return await self._s.get(WorkflowTrigger, trigger_id)

    async def matching_event_triggers(
        self, *, owner_id: uuid.UUID, event_type: str, payload: dict[str, Any]
    ) -> Sequence[WorkflowTrigger]:
        """Enabled event triggers of live, unpaused workflows whose filter matches.

        Matching happens in Postgres: ``payload @> event_filter`` is JSONB containment,
        so ``{"source": "website"}`` matches any payload that has that key and value.
        """
        stmt = (
            sa.select(WorkflowTrigger)
            .join(Workflow, Workflow.id == WorkflowTrigger.workflow_id)
            .where(
                Workflow.owner_id == owner_id,
                Workflow.is_paused.is_(False),
                Workflow.deleted_at.is_(None),
                WorkflowTrigger.type == TriggerType.EVENT,
                WorkflowTrigger.is_enabled.is_(True),
                WorkflowTrigger.event_type == event_type,
                sa.or_(
                    WorkflowTrigger.event_filter.is_(None),
                    sa.cast(payload, JSONB).op("@>")(WorkflowTrigger.event_filter),
                ),
            )
            .order_by(WorkflowTrigger.workflow_id)  # stable lock order across concurrent events
        )
        return (await self._s.scalars(stmt)).all()


_OPEN_TASK_STATUSES = (TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.UP_FOR_RETRY)


class SqlAlchemyRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def overview(
        self, owner_id: uuid.UUID, *, since: datetime, bucket: str
    ) -> dict[str, Any]:
        """Dashboard aggregates in four indexed queries. Everything is computed in
        Postgres; only summary rows cross the wire."""
        owned = (
            sa.select(WorkflowRun)
            .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
            .where(Workflow.owner_id == owner_id)
        )
        window = owned.where(WorkflowRun.created_at >= since).subquery()

        status_rows = await self._s.execute(
            sa.select(window.c.status, sa.func.count()).group_by(window.c.status)
        )
        by_status: dict[Any, int] = {row[0]: row[1] for row in status_rows}
        seconds = sa.func.extract("epoch", window.c.finished_at - window.c.started_at)
        p50, p95 = (
            await self._s.execute(
                sa.select(
                    sa.func.percentile_cont(0.5).within_group(seconds),
                    sa.func.percentile_cont(0.95).within_group(seconds),
                ).where(window.c.finished_at.is_not(None), window.c.started_at.is_not(None))
            )
        ).one()
        slot = sa.func.date_trunc(bucket, window.c.created_at)
        series = (
            await self._s.execute(
                sa.select(slot.label("slot"), window.c.status, sa.func.count())
                .group_by(slot, window.c.status)
                .order_by(slot)
            )
        ).all()
        failed = sa.func.count().filter(window.c.status == RunStatus.FAILED.value)
        failing = (
            await self._s.execute(
                sa.select(Workflow.id, Workflow.name, failed.label("failed"), sa.func.count())
                .join(window, window.c.workflow_id == Workflow.id)
                .group_by(Workflow.id, Workflow.name)
                .having(failed > 0)
                .order_by(failed.desc(), Workflow.name)
                .limit(5)
            )
        ).all()
        active = await self._s.scalar(
            sa.select(sa.func.count()).select_from(
                owned.where(
                    WorkflowRun.status.in_([s for s in RunStatus if not s.is_terminal])
                ).subquery()
            )
        )
        queued = await self._s.scalar(
            sa.select(sa.func.count())
            .select_from(TaskRun)
            .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
            .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
            .where(Workflow.owner_id == owner_id, TaskRun.status == TaskStatus.QUEUED)
        )
        return {
            "by_status": {str(getattr(k, "value", k)): v for k, v in by_status.items()},
            "p50": p50,
            "p95": p95,
            "series": [(row[0], str(getattr(row[1], "value", row[1])), row[2]) for row in series],
            "failing": [
                {"workflow_id": r[0], "name": r[1], "failed": r[2], "total": r[3]} for r in failing
            ],
            "active_runs": active or 0,
            "queued_tasks": queued or 0,
        }

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

    async def count_admitted_active(self, workflow_id: uuid.UUID) -> int:
        # Served by the partial index ix_workflow_runs_active. Waiting runs don't count.
        stmt = sa.select(sa.func.count()).where(
            WorkflowRun.workflow_id == workflow_id,
            WorkflowRun.status.in_((RunStatus.PENDING, RunStatus.RUNNING)),
            WorkflowRun.admitted_at.is_not(None),
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


class SqlAlchemyEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def add(self, event: Event) -> None:
        self._s.add(event)
        await self._s.flush()

    async def get_by_idempotency_key(self, owner_id: uuid.UUID, key: str) -> Event | None:
        stmt = sa.select(Event).where(Event.owner_id == owner_id, Event.idempotency_key == key)
        return (await self._s.scalars(stmt)).one_or_none()

    async def runs_for_event(self, event_id: uuid.UUID) -> Sequence[WorkflowRun]:
        stmt = (
            sa.select(WorkflowRun).where(WorkflowRun.event_id == event_id).order_by(WorkflowRun.id)
        )
        return (await self._s.scalars(stmt)).all()


class SqlAlchemyIntegrationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def _add(self, obj: Any) -> None:
        self._s.add(obj)
        await self._s.flush()

    async def _delete(self, obj: Any) -> None:
        await self._s.delete(obj)
        await self._s.flush()

    # ---- connections -------------------------------------------------- #
    async def add_connection(self, connection: Connection) -> None:
        await self._add(connection)

    async def get_connection(self, connection_id: uuid.UUID) -> Connection | None:
        return await self._s.get(Connection, connection_id)

    async def get_connection_by_name(self, owner_id: uuid.UUID, name: str) -> Connection | None:
        stmt = sa.select(Connection).where(Connection.owner_id == owner_id, Connection.name == name)
        return (await self._s.scalars(stmt)).one_or_none()

    async def list_connections(self, owner_id: uuid.UUID) -> Sequence[Connection]:
        stmt = (
            sa.select(Connection).where(Connection.owner_id == owner_id).order_by(Connection.name)
        )
        return (await self._s.scalars(stmt)).all()

    async def delete_connection(self, connection: Connection) -> None:
        await self._delete(connection)

    async def connection_in_use(self, connection_id: uuid.UUID) -> bool:
        stmt = sa.select(sa.exists().where(NotificationChannel.connection_id == connection_id))
        return bool(await self._s.scalar(stmt))

    # ---- channels ----------------------------------------------------- #
    async def add_channel(self, channel: NotificationChannel) -> None:
        await self._add(channel)

    async def get_channel(self, channel_id: uuid.UUID) -> NotificationChannel | None:
        return await self._s.get(NotificationChannel, channel_id)

    async def channels_by_name(
        self, owner_id: uuid.UUID, names: Sequence[str]
    ) -> dict[str, NotificationChannel]:
        if not names:
            return {}
        stmt = sa.select(NotificationChannel).where(
            NotificationChannel.owner_id == owner_id, NotificationChannel.name.in_(names)
        )
        return {c.name: c for c in (await self._s.scalars(stmt)).all()}

    async def list_channels(self, owner_id: uuid.UUID) -> Sequence[NotificationChannel]:
        stmt = (
            sa.select(NotificationChannel)
            .where(NotificationChannel.owner_id == owner_id)
            .order_by(NotificationChannel.name)
        )
        return (await self._s.scalars(stmt)).all()

    async def delete_channel(self, channel: NotificationChannel) -> None:
        await self._delete(channel)

    # ---- subscriptions ------------------------------------------------ #
    async def list_subscriptions(
        self, workflow_id: uuid.UUID
    ) -> Sequence[NotificationSubscription]:
        stmt = sa.select(NotificationSubscription).where(
            NotificationSubscription.workflow_id == workflow_id
        )
        return (await self._s.scalars(stmt)).all()

    async def add_subscription(self, subscription: NotificationSubscription) -> None:
        await self._add(subscription)

    async def delete_subscription(self, subscription: NotificationSubscription) -> None:
        await self._delete(subscription)

    # ---- deliveries & usage ------------------------------------------- #
    async def add_delivery(self, delivery: NotificationDelivery) -> None:
        await self._add(delivery)

    async def list_deliveries(
        self, owner_id: uuid.UUID, *, channel_id: uuid.UUID | None, limit: int
    ) -> Sequence[NotificationDelivery]:
        stmt = (
            sa.select(NotificationDelivery)
            .join(NotificationChannel, NotificationChannel.id == NotificationDelivery.channel_id)
            .where(NotificationChannel.owner_id == owner_id)
            .order_by(NotificationDelivery.id.desc())
            .limit(limit)
        )
        if channel_id is not None:
            stmt = stmt.where(NotificationDelivery.channel_id == channel_id)
        return (await self._s.scalars(stmt)).all()

    async def llm_usage(self, owner_id: uuid.UUID, *, since: datetime) -> list[dict[str, Any]]:
        """Token and cost totals from ``task_attempts.metrics.llm``, grouped by model.

        Aggregated in Postgres: only one row per (provider, model) crosses the wire.
        """
        llm = TaskAttempt.metrics["llm"]
        provider = llm["provider"].astext
        model = llm["model"].astext

        def total(key: str) -> Any:
            return sa.func.coalesce(sa.func.sum(llm[key].astext.cast(sa.Numeric)), 0)

        stmt = (
            sa.select(
                provider.label("provider"),
                model.label("model"),
                sa.func.count().label("calls"),
                total("input_tokens").label("input_tokens"),
                total("output_tokens").label("output_tokens"),
                total("cost_usd").label("cost_usd"),
                sa.func.count(llm["cost_usd"]).label("priced_calls"),
            )
            .join(TaskRun, TaskRun.id == TaskAttempt.task_run_id)
            .join(WorkflowRun, WorkflowRun.id == TaskRun.workflow_run_id)
            .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
            .where(
                Workflow.owner_id == owner_id,
                TaskAttempt.started_at >= since,
                TaskAttempt.metrics.has_key("llm"),
            )
            .group_by(provider, model)
            .order_by(sa.desc("cost_usd"), provider, model)
        )
        return [dict(row._mapping) for row in (await self._s.execute(stmt)).all()]


class SqlAlchemyUnitOfWork:
    """Wraps one ``AsyncSession``. The session's lifetime is owned by the caller."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = SqlAlchemyUserRepository(session)
        self.refresh_tokens = SqlAlchemyRefreshTokenRepository(session)
        self.api_keys = SqlAlchemyApiKeyRepository(session)
        self.workflows = SqlAlchemyWorkflowRepository(session)
        self.runs = SqlAlchemyRunRepository(session)
        self.events = SqlAlchemyEventRepository(session)
        self.integrations = SqlAlchemyIntegrationRepository(session)

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
