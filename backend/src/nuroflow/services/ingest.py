"""Inbound triggers: events (API-key authenticated) and webhooks (HMAC signed).

Both create runs through :class:`RunLauncher` with ``reject_when_full=False``. Automation
must never drop work, so a busy workflow *queues* the run and the scheduler admits it
when a slot frees up.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from nuroflow.core.clock import Clock
from nuroflow.core.crypto import SecretBox
from nuroflow.core.exceptions import AuthenticationError, Conflict, PermissionDenied
from nuroflow.core.ids import uuid7
from nuroflow.db.models import Event, WorkflowRun
from nuroflow.domain.auth import Principal, Scope
from nuroflow.domain.enums import RunTriggerType, TriggerType
from nuroflow.engine.dispatch import EnqueueRequest, TaskDispatcher
from nuroflow.engine.planner import RunTrigger
from nuroflow.operators.registry import OperatorRegistry
from nuroflow.repositories.interfaces import WorkflowUnitOfWork
from nuroflow.services.launcher import RunLauncher

logger = logging.getLogger(__name__)

SIGNATURE_PREFIX = "sha256="


@dataclass(frozen=True, slots=True)
class IngestedEvent:
    event: Event
    runs: Sequence[WorkflowRun]
    duplicate: bool


def sign_webhook(secret: str, timestamp: str, body: bytes) -> str:
    """The signature senders must compute: HMAC-SHA256 over ``"{timestamp}.{body}"``."""
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return SIGNATURE_PREFIX + mac.hexdigest()


class IngestService:
    def __init__(
        self,
        uow: WorkflowUnitOfWork,
        *,
        registry: OperatorRegistry,
        dispatcher: TaskDispatcher,
        clock: Clock,
        secrets: SecretBox,
        webhook_tolerance_seconds: int,
    ) -> None:
        self._uow = uow
        self._registry = registry
        self._dispatcher = dispatcher
        self._clock = clock
        self._secrets = secrets
        self._tolerance = webhook_tolerance_seconds

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    async def ingest_event(
        self,
        principal: Principal,
        *,
        event_type: str,
        payload: dict[str, Any],
        source: str | None,
        idempotency_key: str | None,
    ) -> IngestedEvent:
        """Store the event, then start a run for every matching event trigger (fan-out)."""
        if not principal.has(Scope.EVENTS_WRITE):
            raise PermissionDenied("Missing required scope: events:write", scope="events:write")
        if idempotency_key and (
            existing := await self._uow.events.get_by_idempotency_key(
                principal.user_id, idempotency_key
            )
        ):
            return IngestedEvent(
                existing, await self._uow.events.runs_for_event(existing.id), duplicate=True
            )

        now = self._clock.now()
        event = Event(
            id=uuid7(),
            owner_id=principal.user_id,
            type=event_type,
            source=source,
            payload=payload,
            idempotency_key=idempotency_key,
            api_key_id=principal.api_key_id,
            received_at=now,
        )
        await self._uow.events.add(event)

        launcher = RunLauncher(self._uow, registry=self._registry, clock=self._clock)
        runs: list[WorkflowRun] = []
        requests: list[EnqueueRequest] = []
        triggers = await self._uow.workflows.matching_event_triggers(
            owner_id=principal.user_id, event_type=event_type, payload=payload
        )
        for trigger in triggers:  # ordered by workflow_id → consistent lock order
            workflow = await self._uow.workflows.get(trigger.workflow_id, lock=True)
            if workflow is None or workflow.is_paused:
                continue
            launch = await launcher.launch(
                workflow,
                RunTrigger(
                    type=RunTriggerType.EVENT,
                    payload=payload,
                    trigger_id=trigger.id,
                    event_id=event.id,
                    idempotency_key=f"event:{event.id}:{trigger.id}",
                ),
                reject_when_full=False,
            )
            runs.append(launch.run)
            requests += launch.requests
        event.processed_at = now
        await self._uow.commit()
        logger.info(
            "event ingested",
            extra={"event_id": str(event.id), "type": event_type, "runs": len(runs)},
        )
        await asyncio.to_thread(self._dispatcher.enqueue, requests)
        return IngestedEvent(event, runs, duplicate=False)

    # ------------------------------------------------------------------ #
    # Webhooks
    # ------------------------------------------------------------------ #
    async def receive_webhook(
        self,
        trigger_id: uuid.UUID,
        *,
        body: bytes,
        payload: dict[str, Any],
        timestamp: str | None,
        signature: str | None,
        delivery_id: str | None,
    ) -> tuple[WorkflowRun, bool]:
        """Verify the signature, then start (or dedupe) a run. Returns (run, created).

        Every verification failure gives the same 401, so an unauthenticated caller
        can't distinguish "no such trigger" from "bad signature".
        """
        invalid = AuthenticationError("Invalid webhook signature")
        trigger = await self._uow.workflows.get_trigger(trigger_id)
        if (
            trigger is None
            or trigger.type is not TriggerType.WEBHOOK
            or not trigger.is_enabled
            or trigger.webhook_secret_encrypted is None
            or not timestamp
            or not signature
        ):
            raise invalid
        try:
            sent_at = int(timestamp)
        except ValueError:
            raise invalid from None
        if abs(self._clock.now().timestamp() - sent_at) > self._tolerance:
            raise AuthenticationError("Webhook timestamp outside the allowed window (replay?)")
        secret = self._secrets.decrypt(trigger.webhook_secret_encrypted)
        if not hmac.compare_digest(sign_webhook(secret, timestamp, body), signature):
            raise invalid

        workflow = await self._uow.workflows.get(trigger.workflow_id, lock=True)
        if workflow is None:
            raise invalid
        if workflow.is_paused:
            raise Conflict("Workflow is paused")  # 409 tells the sender to retry later
        launch = await RunLauncher(self._uow, registry=self._registry, clock=self._clock).launch(
            workflow,
            RunTrigger(
                type=RunTriggerType.WEBHOOK,
                payload=payload,
                trigger_id=trigger.id,
                idempotency_key=f"webhook:{trigger.id}:{delivery_id}" if delivery_id else None,
            ),
            reject_when_full=False,
        )
        if launch.created:
            await self._uow.commit()
            await asyncio.to_thread(self._dispatcher.enqueue, launch.requests)
        return launch.run, launch.created
