"""/events, /hooks and trigger management: how the outside world starts workflows."""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response, status

from nuroflow.api.deps import (
    Container,
    IngestServiceDep,
    WorkflowServiceDep,
    get_client_info,
    require_scope,
)
from nuroflow.api.v1.schemas.runs import RunRead
from nuroflow.api.v1.schemas.triggers import (
    EventAccepted,
    EventIn,
    TriggerRead,
    WebhookAccepted,
    WebhookSecret,
)
from nuroflow.core.config import Settings
from nuroflow.core.exceptions import RateLimited, ValidationError
from nuroflow.db.models import WorkflowTrigger
from nuroflow.domain.auth import Principal, Scope
from nuroflow.domain.enums import TriggerType
from nuroflow.services.auth import ClientInfo

router = APIRouter(tags=["triggers"])

EventWriter = Annotated[Principal, Depends(require_scope(Scope.EVENTS_WRITE))]
Reader = Annotated[Principal, Depends(require_scope(Scope.WORKFLOWS_READ))]
Writer = Annotated[Principal, Depends(require_scope(Scope.WORKFLOWS_WRITE))]

MAX_WEBHOOK_BYTES = 256 * 1024


def webhook_url(settings: Settings, trigger_id: uuid.UUID) -> str:
    return f"{settings.public_base_url.rstrip('/')}{settings.api_v1_prefix}/hooks/{trigger_id}"


def _trigger_read(trigger: WorkflowTrigger, settings: Settings) -> TriggerRead:
    read = TriggerRead.model_validate(trigger)
    if trigger.type is TriggerType.WEBHOOK:
        read.webhook_url = webhook_url(settings, trigger.id)
    return read


async def _limit(container: Container, key: str, per_minute: int) -> None:
    result = await container.rate_limiter.hit(key, limit=per_minute, window_seconds=60)
    if not result.allowed:
        raise RateLimited("Rate limit exceeded", retry_after=result.retry_after_seconds)


# --------------------------------------------------------------------------- events
@router.post("/events", status_code=status.HTTP_202_ACCEPTED, response_model=EventAccepted)
async def ingest_event(
    body: EventIn,
    principal: EventWriter,
    ingest: IngestServiceDep,
    container: Container,
    response: Response,
    idempotency_key: Annotated[str | None, Header(max_length=200)] = None,
) -> EventAccepted:
    """Publish an event (e.g. ``lead.captured``). Every enabled event trigger whose
    ``event_type`` matches, and whose ``event_filter`` is contained in the payload,
    starts a run. Send an ``Idempotency-Key`` so client retries never duplicate leads."""
    await _limit(
        container,
        f"events:{principal.api_key_id or principal.user_id}",
        container.settings.event_rate_limit_per_minute,
    )
    result = await ingest.ingest_event(
        principal,
        event_type=body.type,
        payload=body.payload,
        source=body.source,
        idempotency_key=idempotency_key,
    )
    if result.duplicate:
        response.status_code = status.HTTP_200_OK
    return EventAccepted(
        event_id=result.event.id,
        duplicate=result.duplicate,
        runs=[RunRead.model_validate(r) for r in result.runs],
    )


# --------------------------------------------------------------------------- webhooks
@router.post(
    "/hooks/{trigger_id}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookAccepted,
    openapi_extra={"security": []},  # authenticated by HMAC signature, not by token
)
async def receive_webhook(
    trigger_id: uuid.UUID,
    request: Request,
    ingest: IngestServiceDep,
    container: Container,
    response: Response,
    client: Annotated[ClientInfo, Depends(get_client_info)],
    x_nuroflow_timestamp: Annotated[str | None, Header()] = None,
    x_nuroflow_signature: Annotated[str | None, Header()] = None,
    x_nuroflow_delivery: Annotated[str | None, Header(max_length=200)] = None,
) -> WebhookAccepted:
    """Signed webhook. Required headers:

    * ``X-NuroFlow-Timestamp``: unix seconds; more than 5 minutes off is rejected
      (replay protection)
    * ``X-NuroFlow-Signature``: ``sha256=<hex HMAC-SHA256(secret, "{timestamp}.{raw body}")>``
    * ``X-NuroFlow-Delivery`` (optional): unique per delivery; retries with the same
      id are deduplicated
    """
    await _limit(
        container, f"webhook:{trigger_id}", container.settings.webhook_rate_limit_per_minute
    )
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        raise ValidationError("Webhook body too large")
    payload = _parse_payload(body)
    run, created = await ingest.receive_webhook(
        trigger_id,
        body=body,
        payload=payload,
        timestamp=x_nuroflow_timestamp,
        signature=x_nuroflow_signature,
        delivery_id=x_nuroflow_delivery,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return WebhookAccepted(run_id=run.id, status=run.status.value, duplicate=not created)


def _parse_payload(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        data = json.loads(body)
    except ValueError:
        return {"body": body.decode("utf-8", errors="replace")}
    return data if isinstance(data, dict) else {"body": data}


# --------------------------------------------------------------------------- trigger admin
@router.get("/workflows/{workflow_id}/triggers", response_model=list[TriggerRead])
async def list_triggers(
    workflow_id: uuid.UUID, principal: Reader, workflows: WorkflowServiceDep, container: Container
) -> list[TriggerRead]:
    """Triggers are declared in the workflow spec; this shows their live state."""
    triggers = await workflows.triggers(principal, workflow_id)
    return [_trigger_read(t, container.settings) for t in triggers]


@router.get("/triggers/{trigger_id}/secret", response_model=WebhookSecret)
async def get_webhook_secret(
    trigger_id: uuid.UUID, principal: Writer, workflows: WorkflowServiceDep, container: Container
) -> WebhookSecret:
    trigger, secret = await workflows.webhook_secret(principal, trigger_id)
    return WebhookSecret(
        trigger_id=trigger.id,
        webhook_url=webhook_url(container.settings, trigger.id),
        secret=secret,
    )


@router.post("/triggers/{trigger_id}:rotate-secret", response_model=WebhookSecret)
async def rotate_webhook_secret(
    trigger_id: uuid.UUID, principal: Writer, workflows: WorkflowServiceDep, container: Container
) -> WebhookSecret:
    """Issue a new signing secret. The old one stops working immediately."""
    trigger, secret = await workflows.webhook_secret(principal, trigger_id, rotate=True)
    return WebhookSecret(
        trigger_id=trigger.id,
        webhook_url=webhook_url(container.settings, trigger.id),
        secret=secret,
    )
