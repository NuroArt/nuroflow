"""In-memory implementations of the repository Protocols.

These let service tests run in milliseconds with no database. They mimic only the
behaviour the services rely on (lookups, row "locks" as no-ops, bulk revocation),
and they're type-checked against the same Protocols as the real repositories.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from nuroflow.core.ids import uuid7
from nuroflow.db.models import ApiKey, RefreshToken, User


class InMemoryUserRepository:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, User] = {}

    async def get(self, user_id: uuid.UUID) -> User | None:
        return self.rows.get(user_id)

    async def get_by_email(self, email: str) -> User | None:
        return next((u for u in self.rows.values() if u.email.lower() == email.lower()), None)

    async def add(self, user: User) -> None:
        user.id = user.id or uuid7()
        user.created_at = user.created_at or datetime.now(UTC)
        self.rows[user.id] = user

    async def list_page(self, *, limit: int, before_id: uuid.UUID | None = None) -> Sequence[User]:
        rows = sorted(self.rows.values(), key=lambda u: u.id, reverse=True)
        if before_id is not None:
            rows = [u for u in rows if u.id < before_id]
        return rows[:limit]


class InMemoryRefreshTokenRepository:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, RefreshToken] = {}

    async def add(self, token: RefreshToken) -> None:
        token.id = token.id or uuid7()
        self.rows[token.id] = token

    async def get_by_hash_for_update(self, token_hash: str) -> RefreshToken | None:
        return next((t for t in self.rows.values() if t.token_hash == token_hash), None)

    async def revoke_family(self, family_id: uuid.UUID, *, at: datetime) -> int:
        return self._revoke(lambda t: t.family_id == family_id, at)

    async def revoke_all_for_user(self, user_id: uuid.UUID, *, at: datetime) -> int:
        return self._revoke(lambda t: t.user_id == user_id, at)

    def _revoke(self, predicate: object, at: datetime) -> int:
        count = 0
        for token in self.rows.values():
            if token.revoked_at is None and predicate(token):  # type: ignore[operator]
                token.revoked_at = at
                count += 1
        return count


class InMemoryApiKeyRepository:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, ApiKey] = {}

    async def add(self, api_key: ApiKey) -> None:
        api_key.id = api_key.id or uuid7()
        self.rows[api_key.id] = api_key

    async def get(self, key_id: uuid.UUID) -> ApiKey | None:
        return self.rows.get(key_id)

    async def get_by_prefix(self, prefix: str) -> ApiKey | None:
        return next((k for k in self.rows.values() if k.prefix == prefix), None)

    async def list_for_user(self, user_id: uuid.UUID) -> Sequence[ApiKey]:
        return [k for k in self.rows.values() if k.user_id == user_id]


class InMemoryUnitOfWork:
    def __init__(self) -> None:
        self.users = InMemoryUserRepository()
        self.refresh_tokens = InMemoryRefreshTokenRepository()
        self.api_keys = InMemoryApiKeyRepository()
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:  # in-memory: nothing to undo
        pass
