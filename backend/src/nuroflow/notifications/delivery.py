"""Claiming and delivering outbox rows (sync: runs in scheduler and worker processes).

Delivery protocol, mirroring the task executor:

1. **Claim** (scheduler): ``FOR UPDATE SKIP LOCKED`` on due pending rows, bump
   ``attempt_count`` and push ``next_attempt_at`` out by a *lease*. A worker that
   dies mid-send simply lets the lease expire, and the row is claimed again: no reaper.
2. **Send** (worker): load channel and credentials, send, with no transaction open.
3. **Record**: only if ``attempt_count`` still equals the claimed attempt (fencing),
   mark it sent, schedule a retry with backoff, or give up after max attempts.

Guarantee: at-least-once. A duplicate alert is acceptable, a lost one is not.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

import httpx
import sqlalchemy as sa
from celery import Celery
from sqlalchemy.orm import Session, sessionmaker

from nuroflow.core.clock import Clock
from nuroflow.core.config import Settings
from nuroflow.core.crypto import SecretBox
from nuroflow.db.models import Connection, NotificationChannel, NotificationDelivery
from nuroflow.domain.enums import ChannelType, DeliveryStatus
from nuroflow.engine.retry import RetryPolicy
from nuroflow.integrations.http_errors import is_retryable_status
from nuroflow.integrations.telegram.base import TelegramClient, TelegramError, TelegramMessage
from nuroflow.notifications.formatting import telegram_html
from nuroflow.observability import metrics
from nuroflow.operators.base import TaskError
from nuroflow.operators.builtin.http import assert_public_url
from nuroflow.services.ingest import sign_webhook

logger = logging.getLogger(__name__)

DELIVER_NOTIFICATION = "nuroflow.deliver_notification"
NOTIFICATION_QUEUE = "notifications"
LEASE = timedelta(minutes=2)
MAX_ERROR_CHARS = 2000


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    delivery_id: uuid.UUID
    attempt: int


class NotificationSender(Protocol):
    def send(self, claims: Sequence[DeliveryClaim]) -> None: ...


class CelerySender:
    def __init__(self, app: Celery) -> None:
        self._app = app

    def send(self, claims: Sequence[DeliveryClaim]) -> None:
        for claim in claims:
            try:
                self._app.send_task(
                    DELIVER_NOTIFICATION,
                    args=[str(claim.delivery_id), claim.attempt],
                    queue=NOTIFICATION_QUEUE,
                    soft_time_limit=60,
                    time_limit=90,
                )
            except Exception:  # the lease expires and the row is claimed again
                logger.exception(
                    "notification dispatch failed", extra={"id": str(claim.delivery_id)}
                )


class DeliveryError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def claim_due(session: Session, *, now: Any, limit: int) -> list[DeliveryClaim]:
    rows = session.scalars(
        sa.select(NotificationDelivery)
        .where(
            NotificationDelivery.status == DeliveryStatus.PENDING,
            NotificationDelivery.next_attempt_at <= now,
        )
        .order_by(NotificationDelivery.next_attempt_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    claims = []
    for row in rows:
        row.attempt_count += 1
        row.next_attempt_at = now + LEASE
        claims.append(DeliveryClaim(row.id, row.attempt_count))
    return claims


@dataclass(frozen=True, slots=True)
class _Prepared:
    channel_type: ChannelType
    target: str
    secret: str | None
    payload: dict[str, Any]


class NotificationTransport:
    """Sends one notification: Telegram (HTML) or a JSON webhook (optionally signed).

    Stateless apart from its HTTP client, so the API's ``:test`` endpoint and the
    worker's deliverer share exactly the same code path.
    """

    def __init__(
        self,
        *,
        telegram: TelegramClient,
        clock: Clock,
        public_base_url: str,
        default_bot_token: str | None = None,
        allow_private_networks: bool = False,
        http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._telegram = telegram
        self._clock = clock
        self._public_base_url = public_base_url
        self._default_bot_token = default_bot_token
        self._allow_private = allow_private_networks
        self._http = httpx.Client(timeout=10.0, transport=http_transport)

    def send(
        self, channel_type: ChannelType, target: str, secret: str | None, payload: dict[str, Any]
    ) -> None:
        if channel_type is ChannelType.TELEGRAM:
            self._send_telegram(target, secret, payload)
        else:
            self._send_webhook(target, secret, payload)

    # ---- send (no transaction open) ------------------------------------ #
    def _send_telegram(self, chat_id: str, token: str | None, payload: dict[str, Any]) -> None:
        token = token or self._default_bot_token
        if self._telegram.requires_credentials and not token:
            raise DeliveryError(
                "telegram channel has no connection and no platform bot token", retryable=False
            )
        text = telegram_html(payload, public_base_url=self._public_base_url)
        try:
            self._telegram.send_message(
                TelegramMessage(chat_id=chat_id, text=text, parse_mode="HTML"), bot_token=token
            )
        except TelegramError as exc:
            raise DeliveryError(str(exc), retryable=exc.retryable) from exc

    def _send_webhook(self, url: str, secret: str | None, payload: dict[str, Any]) -> None:
        if not self._allow_private:
            try:
                assert_public_url(url)
            except TaskError as exc:
                raise DeliveryError(str(exc), retryable=exc.retryable) from exc
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "NuroFlow-Notifier/1"}
        if secret:
            ts = str(int(self._clock.now().timestamp()))
            headers |= {
                "X-NuroFlow-Timestamp": ts,
                "X-NuroFlow-Signature": sign_webhook(secret, ts, body),
            }
        try:
            response = self._http.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise DeliveryError(
                f"webhook connection error: {type(exc).__name__}", retryable=True
            ) from exc
        if response.status_code >= 300:
            raise DeliveryError(
                f"webhook HTTP {response.status_code}",
                retryable=is_retryable_status(response.status_code),
            )


class NotificationDeliverer:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        transport: NotificationTransport,
        secrets: SecretBox,
        clock: Clock,
        max_attempts: int,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._sessions = session_factory
        self._transport = transport
        self._secrets = secrets
        self._clock = clock
        self._max_attempts = max_attempts
        self._retry = retry or RetryPolicy(base_seconds=30, max_seconds=900)

    def deliver(self, claim: DeliveryClaim) -> DeliveryStatus | None:
        prepared = self._prepare(claim)
        if prepared is None:
            return None
        error: DeliveryError | None = None
        try:
            self._transport.send(
                prepared.channel_type, prepared.target, prepared.secret, prepared.payload
            )
        except DeliveryError as exc:
            error = exc
        status = self._record(claim, error)
        if status is not None:
            outcome = (
                "sent" if error is None else "retrying" if status.value == "pending" else "failed"
            )
            metrics.NOTIFICATIONS.labels(
                channel_type=prepared.channel_type.value, outcome=outcome
            ).inc()
        return status

    # ---- T1: load everything, then release the connection -------------- #
    def _prepare(self, claim: DeliveryClaim) -> _Prepared | None:
        with self._sessions() as session:
            delivery = session.get(NotificationDelivery, claim.delivery_id)
            if (
                delivery is None
                or delivery.status is not DeliveryStatus.PENDING
                or delivery.attempt_count != claim.attempt
            ):
                logger.info(
                    "stale notification claim ignored", extra={"id": str(claim.delivery_id)}
                )
                return None
            channel = session.get(NotificationChannel, delivery.channel_id)
            assert channel is not None  # FK + ON DELETE CASCADE
            secret = None
            if channel.connection_id is not None:
                connection = session.get(Connection, channel.connection_id)
                assert connection is not None  # FK RESTRICT
                secret = self._secrets.decrypt(connection.encrypted_secret)
            return _Prepared(channel.type, channel.target, secret, dict(delivery.payload))

    # ---- T2: record with fencing ---------------------------------------- #
    def _record(self, claim: DeliveryClaim, error: DeliveryError | None) -> DeliveryStatus | None:
        now = self._clock.now()
        with self._sessions() as session, session.begin():
            delivery = session.get(NotificationDelivery, claim.delivery_id, with_for_update=True)
            if (
                delivery is None
                or delivery.status is not DeliveryStatus.PENDING
                or delivery.attempt_count != claim.attempt
            ):
                return None
            if error is None:
                delivery.status, delivery.sent_at, delivery.last_error = (
                    DeliveryStatus.SENT,
                    now,
                    None,
                )
                delivery.next_attempt_at = None
            else:
                delivery.last_error = str(error)[:MAX_ERROR_CHARS]
                if error.retryable and claim.attempt < self._max_attempts:
                    delivery.next_attempt_at = now + self._retry.delay(claim.attempt)
                else:
                    delivery.status, delivery.next_attempt_at = DeliveryStatus.FAILED, None
                logger.warning(
                    "notification delivery failed",
                    extra={
                        "id": str(delivery.id),
                        "attempt": claim.attempt,
                        "will_retry": delivery.status is DeliveryStatus.PENDING,
                    },
                )
            return delivery.status


def build_transport(
    settings: Settings, *, telegram: TelegramClient, clock: Clock
) -> NotificationTransport:
    token = settings.telegram_bot_token
    return NotificationTransport(
        telegram=telegram,
        clock=clock,
        public_base_url=settings.public_base_url,
        default_bot_token=token.get_secret_value() if token else None,
        allow_private_networks=settings.http_allow_private_networks,
    )
