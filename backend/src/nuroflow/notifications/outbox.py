"""``emit()``: record notification intents inside the caller's transaction (sync engine side)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from nuroflow.core.ids import uuid7
from nuroflow.db.models import (
    NotificationChannel,
    NotificationDelivery,
    NotificationSubscription,
    Workflow,
    WorkflowRun,
)
from nuroflow.domain.enums import RunEvent

logger = logging.getLogger(__name__)


def emit(
    session: Session,
    run: WorkflowRun,
    event: RunEvent,
    *,
    now: datetime,
    task_key: str | None = None,
    attempt: int | None = None,
    error: str | None = None,
) -> int:
    """Queue one delivery per enabled channel subscribed to ``event``. Returns the count.

    Cheap when nobody listens: one indexed query, no inserts. The payload is a
    snapshot, so a later rename of the workflow doesn't rewrite alert history.
    """
    rows = session.execute(
        sa.select(NotificationChannel.id, Workflow.slug, Workflow.name)
        .join(
            NotificationSubscription,
            NotificationSubscription.channel_id == NotificationChannel.id,
        )
        .join(Workflow, Workflow.id == NotificationSubscription.workflow_id)
        .where(
            NotificationSubscription.workflow_id == run.workflow_id,
            NotificationSubscription.is_enabled,
            NotificationChannel.is_enabled,
            sa.literal(event.value) == sa.any_(NotificationSubscription.events),
        )
    ).all()
    for channel_id, slug, name in rows:
        payload: dict[str, Any] = {
            "event": event.value,
            "workflow": {"id": str(run.workflow_id), "slug": slug, "name": name},
            "run": {
                "id": str(run.id),
                "status": run.status.value,
                "trigger_type": run.trigger_type.value,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            },
            "error": (error or run.error_message or None),
        }
        if task_key is not None:
            payload["task"] = {"key": task_key, "attempt": attempt}
        session.add(
            NotificationDelivery(
                id=uuid7(),
                channel_id=channel_id,
                workflow_run_id=run.id,
                event=event,
                payload=payload,
                # The engine's clock, not the DB's now(): keeps time injectable (FrozenClock).
                next_attempt_at=now,
            )
        )
    if rows:
        logger.info(
            "notifications queued",
            extra={"run_id": str(run.id), "event": event.value, "channels": len(rows)},
        )
    return len(rows)
