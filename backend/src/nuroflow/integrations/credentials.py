"""Credential resolution: which secret does this call use?

Order, most specific first:

1. ``connection: <name>`` on the task: the **workflow owner's** encrypted connection.
   Ownership is enforced in SQL, so a workflow can never borrow another user's key.
2. The platform default from settings (``NUROFLOW_OPENAI_API_KEY`` …).
3. Otherwise a permanent error that tells the author exactly how to fix it.

Secrets are decrypted only here, only at the moment of use, and never logged.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from nuroflow.core.config import Settings
from nuroflow.core.crypto import SecretBox
from nuroflow.db.models import Connection
from nuroflow.domain.enums import ConnectionType


class CredentialError(Exception):
    """Missing or mismatched credentials: a configuration problem, never retryable."""


@dataclass(frozen=True, slots=True)
class Credential:
    secret: str
    source: Literal["connection", "platform"]
    config: Mapping[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:  # never print secrets, even in debug output
        return f"Credential(source={self.source!r}, secret='***')"


class CredentialResolver(Protocol):
    def resolve(
        self, *, owner_id: uuid.UUID, kind: ConnectionType, name: str | None
    ) -> Credential: ...


class SqlCredentialResolver:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        secrets: SecretBox,
        defaults: Mapping[ConnectionType, str | None],
    ) -> None:
        self._sessions = session_factory
        self._secrets = secrets
        self._defaults = defaults

    @classmethod
    def from_settings(
        cls, session_factory: sessionmaker[Session], settings: Settings, secrets: SecretBox
    ) -> SqlCredentialResolver:
        def raw(value: Any) -> str | None:
            return value.get_secret_value() if value is not None else None

        return cls(
            session_factory,
            secrets=secrets,
            defaults={
                ConnectionType.OPENAI: raw(settings.openai_api_key),
                ConnectionType.ANTHROPIC: raw(settings.anthropic_api_key),
                ConnectionType.TELEGRAM: raw(settings.telegram_bot_token),
            },
        )

    def resolve(self, *, owner_id: uuid.UUID, kind: ConnectionType, name: str | None) -> Credential:
        if name is not None:
            with self._sessions() as session:
                row = session.scalars(
                    sa.select(Connection).where(
                        Connection.owner_id == owner_id, Connection.name == name
                    )
                ).one_or_none()
            if row is None:
                raise CredentialError(f"connection '{name}' not found")
            if row.type is not kind:
                raise CredentialError(
                    f"connection '{name}' is a {row.type.value} connection, not {kind.value}"
                )
            return Credential(
                secret=self._secrets.decrypt(row.encrypted_secret),
                source="connection",
                config=dict(row.config),
            )
        default = self._defaults.get(kind)
        if default:
            return Credential(secret=default, source="platform")
        raise CredentialError(
            f"no {kind.value} credentials: add `connection: <name>` to the task "
            f"(POST /api/v1/connections) or set NUROFLOW_{kind.value.upper()}_"
            f"{'BOT_TOKEN' if kind is ConnectionType.TELEGRAM else 'API_KEY'}"
        )


class StaticCredentialResolver:
    """In-memory resolver for tests: ``{(kind, name): secret}``; name ``None`` = default."""

    def __init__(self, secrets: Mapping[tuple[ConnectionType, str | None], str]) -> None:
        self._secrets = secrets

    def resolve(self, *, owner_id: uuid.UUID, kind: ConnectionType, name: str | None) -> Credential:
        try:
            secret = self._secrets[(kind, name)]
        except KeyError:
            raise CredentialError(f"no {kind.value} credentials for {name!r}") from None
        return Credential(secret=secret, source="connection" if name else "platform")
