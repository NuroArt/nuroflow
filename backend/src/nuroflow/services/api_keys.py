"""API key use cases: create, list, revoke, authenticate."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from nuroflow.core.clock import Clock
from nuroflow.core.exceptions import (
    AuthenticationError,
    NotFound,
    PermissionDenied,
    ValidationError,
)
from nuroflow.core.ids import uuid7
from nuroflow.core.security import ApiKeyCodec
from nuroflow.db.models import ApiKey
from nuroflow.domain.auth import API_KEY_FORBIDDEN_SCOPES, ROLE_SCOPES, AuthMethod, Principal, Scope
from nuroflow.repositories.interfaces import UnitOfWork

logger = logging.getLogger(__name__)

MAX_KEYS_PER_USER = 25
# Writing last_used_at on every request would turn every read into a write; once a
# minute is precise enough for "is this key still in use?"
LAST_USED_RESOLUTION = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class CreatedApiKey:
    api_key: ApiKey
    plaintext: str  # returned to the caller exactly once


class ApiKeyService:
    def __init__(self, uow: UnitOfWork, *, codec: ApiKeyCodec, clock: Clock) -> None:
        self._uow = uow
        self._codec = codec
        self._clock = clock

    async def create(
        self,
        principal: Principal,
        *,
        name: str,
        scopes: Sequence[Scope],
        expires_in_days: int | None = None,
    ) -> CreatedApiKey:
        if principal.method is not AuthMethod.JWT:
            raise PermissionDenied("API keys can only be created from an interactive login")
        requested = frozenset(scopes)
        if not requested:
            raise ValidationError("At least one scope is required", field="scopes")
        if forbidden := requested & API_KEY_FORBIDDEN_SCOPES:
            raise ValidationError(
                f"Scopes not allowed on API keys: {sorted(forbidden)}", field="scopes"
            )
        if excess := requested - ROLE_SCOPES[principal.role]:
            raise PermissionDenied(f"Your role cannot grant: {sorted(excess)}")
        existing = await self._uow.api_keys.list_for_user(principal.user_id)
        if sum(1 for k in existing if k.revoked_at is None) >= MAX_KEYS_PER_USER:
            raise ValidationError(f"Limit of {MAX_KEYS_PER_USER} active API keys reached")

        generated = self._codec.generate()
        now = self._clock.now()
        api_key = ApiKey(
            id=uuid7(),
            user_id=principal.user_id,
            name=name.strip(),
            prefix=generated.prefix,
            key_hash=generated.secret_hash,
            scopes=sorted(s.value for s in requested),
            expires_at=now + timedelta(days=expires_in_days) if expires_in_days else None,
        )
        await self._uow.api_keys.add(api_key)
        await self._uow.commit()
        logger.info(
            "api key created", extra={"user_id": str(principal.user_id), "prefix": generated.prefix}
        )
        return CreatedApiKey(api_key=api_key, plaintext=generated.plaintext)

    async def list(self, principal: Principal) -> Sequence[ApiKey]:
        return await self._uow.api_keys.list_for_user(principal.user_id)

    async def revoke(self, principal: Principal, key_id: uuid.UUID) -> None:
        api_key = await self._uow.api_keys.get(key_id)
        # 404 (not 403) for other users' keys: don't confirm that the ID exists.
        if api_key is None or not principal.can_access_owned_by(api_key.user_id):
            raise NotFound("API key not found")
        if api_key.revoked_at is None:
            api_key.revoked_at = self._clock.now()
            await self._uow.commit()

    async def authenticate(self, plaintext: str) -> Principal:
        prefix, secret = self._codec.parse(plaintext)
        api_key = await self._uow.api_keys.get_by_prefix(prefix)
        # Same generic message for every failure, so callers learn nothing about which check failed.
        invalid = AuthenticationError("Invalid API key")
        if api_key is None or not self._codec.verify(secret, api_key.key_hash):
            raise invalid
        now = self._clock.now()
        if api_key.revoked_at is not None or (api_key.expires_at and api_key.expires_at <= now):
            raise invalid
        owner = await self._uow.users.get(api_key.user_id)
        if owner is None or not owner.is_active:
            raise invalid

        if api_key.last_used_at is None or now - api_key.last_used_at >= LAST_USED_RESOLUTION:
            api_key.last_used_at = now
            await self._uow.commit()

        scopes = frozenset(Scope(s) for s in api_key.scopes if s in Scope._value2member_map_)
        return Principal.for_api_key(owner.id, owner.role, api_key.id, scopes)
