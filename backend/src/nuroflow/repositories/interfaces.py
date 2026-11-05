"""Repository and Unit-of-Work contracts (Dependency Inversion).

Services depend on these Protocols, never on SQLAlchemy sessions. Two implementations
exist:

* ``nuroflow.repositories.sqlalchemy``: the real thing (async, Postgres).
* ``tests/fakes.py``: in-memory, used to unit-test services in milliseconds.

Pragmatic note: the "entities" passed through these interfaces are the ORM models
themselves. A fully separate domain-entity layer plus mappers doubles the code for
little gain at this size. The rule we keep is the important one: **no service
imports SQLAlchemy**. If the domain later grows rich behaviour, introducing
dataclass entities is a local change behind these Protocols.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from nuroflow.db.models import ApiKey, RefreshToken, User


class UserRepository(Protocol):
    async def get(self, user_id: uuid.UUID) -> User | None: ...
    async def get_by_email(self, email: str) -> User | None: ...
    async def add(self, user: User) -> None: ...
    async def list_page(
        self, *, limit: int, before_id: uuid.UUID | None = None
    ) -> Sequence[User]: ...


class RefreshTokenRepository(Protocol):
    async def add(self, token: RefreshToken) -> None: ...

    async def get_by_hash_for_update(self, token_hash: str) -> RefreshToken | None:
        """Fetch and row-lock, so concurrent refreshes of one token serialise."""
        ...

    async def revoke_family(self, family_id: uuid.UUID, *, at: datetime) -> int: ...
    async def revoke_all_for_user(self, user_id: uuid.UUID, *, at: datetime) -> int: ...


class ApiKeyRepository(Protocol):
    async def add(self, api_key: ApiKey) -> None: ...
    async def get(self, key_id: uuid.UUID) -> ApiKey | None: ...
    async def get_by_prefix(self, prefix: str) -> ApiKey | None: ...
    async def list_for_user(self, user_id: uuid.UUID) -> Sequence[ApiKey]: ...


class UnitOfWork(Protocol):
    """One transaction. Services call ``commit()`` explicitly at the end of a use case.

    Explicit commits make transaction boundaries visible in the service code, rather
    than hiding them in a request hook that runs "somewhere after" the handler.
    """

    # Read-only properties (not plain attributes) keep the Protocol covariant, so an
    # implementation may expose concrete repository types.
    @property
    def users(self) -> UserRepository: ...
    @property
    def refresh_tokens(self) -> RefreshTokenRepository: ...
    @property
    def api_keys(self) -> ApiKeyRepository: ...

    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
