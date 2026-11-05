"""``data.store``: persist a JSON record into a named collection (exactly once per task run)."""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from pydantic import Field

from nuroflow.operators.base import Operator, OperatorContext, OperatorInput


class DataStore(Protocol):
    def upsert_for_task_run(
        self,
        *,
        owner_id: uuid.UUID,
        collection: str,
        record: dict[str, Any],
        workflow_run_id: uuid.UUID,
        task_run_id: uuid.UUID,
    ) -> uuid.UUID:
        """Insert, or replace the record already written by this task run (idempotent)."""
        ...


class DataStoreInput(OperatorInput):
    collection: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,99}$")
    record: dict[str, Any]


class DataStoreOperator(Operator):
    name = "data.store"
    description = "Save a JSON record to a collection; retries never create duplicates."
    input_model = DataStoreInput

    def __init__(self, store: DataStore) -> None:
        self._store = store

    def execute(self, inputs: DataStoreInput, ctx: OperatorContext) -> dict[str, Any]:
        record_id = self._store.upsert_for_task_run(
            owner_id=ctx.owner_id,
            collection=inputs.collection,
            record=inputs.record,
            workflow_run_id=ctx.run_id,
            task_run_id=ctx.task_run_id,
        )
        ctx.log(f"stored record in '{inputs.collection}'", record_id=str(record_id))
        return {"record_id": str(record_id), "collection": inputs.collection}
