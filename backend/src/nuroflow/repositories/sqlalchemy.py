"""Async SQLAlchemy implementations of the repository Protocols (API process)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from nuroflow.db.models import ApiKey, RefreshToken, User


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


class SqlAlchemyUnitOfWork:
    """Wraps one ``AsyncSession``. The session's lifetime is owned by the caller."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = SqlAlchemyUserRepository(session)
        self.refresh_tokens = SqlAlchemyRefreshTokenRepository(session)
        self.api_keys = SqlAlchemyApiKeyRepository(session)

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()
