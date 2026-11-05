"""SQL implementation of the ``DataStore`` port used by the ``data.store`` operator."""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from nuroflow.core.ids import uuid7
from nuroflow.db.models import DataRecord


class SqlDataStore:
    """Writes in its own short transaction (the operator runs outside the engine's).

    ``ON CONFLICT (task_run_id) DO UPDATE`` makes the write idempotent per task run:
    if the worker crashes after inserting but before recording success, the retry
    overwrites the same row instead of creating a duplicate lead.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def upsert_for_task_run(
        self,
        *,
        owner_id: uuid.UUID,
        collection: str,
        record: dict[str, Any],
        workflow_run_id: uuid.UUID,
        task_run_id: uuid.UUID,
    ) -> uuid.UUID:
        stmt = insert(DataRecord).values(
            id=uuid7(),
            owner_id=owner_id,
            collection=collection,
            record=record,
            workflow_run_id=workflow_run_id,
            task_run_id=task_run_id,
        )
        upsert = stmt.on_conflict_do_update(
            index_elements=[DataRecord.task_run_id],
            set_={
                "record": stmt.excluded.record,
                "collection": stmt.excluded.collection,
                "updated_at": sa.func.now(),
            },
        ).returning(DataRecord.id)
        with self._sessions() as session, session.begin():
            record_id: uuid.UUID = session.execute(upsert).scalar_one()
        return record_id


class UnavailableDataStore:
    """Placeholder for processes that describe operators but never execute them (the API)."""

    def upsert_for_task_run(self, **_: Any) -> uuid.UUID:
        raise RuntimeError("data.store cannot execute in this process")
