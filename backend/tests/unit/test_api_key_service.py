from __future__ import annotations

from datetime import timedelta

import pytest

from nuroflow.core.clock import FrozenClock
from nuroflow.core.exceptions import (
    AuthenticationError,
    NotFound,
    PermissionDenied,
    ValidationError,
)
from nuroflow.core.ids import uuid7
from nuroflow.core.security import ApiKeyCodec
from nuroflow.db.models import User
from nuroflow.domain.auth import AuthMethod, Principal, Scope
from nuroflow.domain.enums import UserRole
from nuroflow.services.api_keys import ApiKeyService
from tests.fakes import InMemoryUnitOfWork


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def uow() -> InMemoryUnitOfWork:
    return InMemoryUnitOfWork()


@pytest.fixture
def service(uow: InMemoryUnitOfWork, clock: FrozenClock) -> ApiKeyService:
    return ApiKeyService(uow, codec=ApiKeyCodec(pepper=b"p" * 32), clock=clock)


async def make_user(uow: InMemoryUnitOfWork, role: UserRole = UserRole.EDITOR) -> User:
    user = User(
        id=uuid7(), email=f"{uuid7().hex}@x.io", hashed_password="h", role=role, is_active=True
    )
    await uow.users.add(user)
    return user


async def test_created_key_authenticates_with_requested_scopes(
    service: ApiKeyService, uow: InMemoryUnitOfWork
) -> None:
    user = await make_user(uow)
    created = await service.create(
        Principal.for_user(user.id, user.role), name="zapier", scopes=[Scope.EVENTS_WRITE]
    )
    principal = await service.authenticate(created.plaintext)
    assert principal.method is AuthMethod.API_KEY
    assert principal.scopes == {Scope.EVENTS_WRITE}
    assert principal.api_key_id == created.api_key.id


async def test_viewer_cannot_grant_write_scopes(
    service: ApiKeyService, uow: InMemoryUnitOfWork
) -> None:
    viewer = await make_user(uow, UserRole.VIEWER)
    with pytest.raises(PermissionDenied, match="cannot grant"):
        await service.create(
            Principal.for_user(viewer.id, viewer.role), name="k", scopes=[Scope.RUNS_WRITE]
        )


async def test_admin_scope_is_never_allowed_on_keys(
    service: ApiKeyService, uow: InMemoryUnitOfWork
) -> None:
    admin = await make_user(uow, UserRole.ADMIN)
    with pytest.raises(ValidationError, match="not allowed"):
        await service.create(
            Principal.for_user(admin.id, admin.role), name="k", scopes=[Scope.USERS_ADMIN]
        )


async def test_api_key_cannot_mint_more_api_keys(
    service: ApiKeyService, uow: InMemoryUnitOfWork
) -> None:
    user = await make_user(uow)
    created = await service.create(
        Principal.for_user(user.id, user.role), name="k", scopes=[Scope.RUNS_READ]
    )
    key_principal = await service.authenticate(created.plaintext)
    with pytest.raises(PermissionDenied, match="interactive login"):
        await service.create(key_principal, name="k2", scopes=[Scope.RUNS_READ])


@pytest.mark.parametrize("failure", ["revoked", "expired", "owner_inactive", "wrong_secret"])
async def test_authentication_failures_are_generic(
    service: ApiKeyService, uow: InMemoryUnitOfWork, clock: FrozenClock, failure: str
) -> None:
    user = await make_user(uow)
    created = await service.create(
        Principal.for_user(user.id, user.role),
        name="k",
        scopes=[Scope.RUNS_READ],
        expires_in_days=1,
    )
    presented = created.plaintext
    if failure == "revoked":
        await service.revoke(Principal.for_user(user.id, user.role), created.api_key.id)
    elif failure == "expired":
        clock.advance(days=2)
    elif failure == "owner_inactive":
        user.is_active = False
    else:
        presented = presented[:-4] + ("AAAA" if not presented.endswith("AAAA") else "BBBB")

    with pytest.raises(AuthenticationError) as exc_info:
        await service.authenticate(presented)
    assert exc_info.value.detail == "Invalid API key"


async def test_demoting_owner_narrows_existing_key(
    service: ApiKeyService, uow: InMemoryUnitOfWork
) -> None:
    user = await make_user(uow, UserRole.EDITOR)
    created = await service.create(
        Principal.for_user(user.id, user.role),
        name="k",
        scopes=[Scope.RUNS_READ, Scope.RUNS_WRITE],
    )
    user.role = UserRole.VIEWER
    principal = await service.authenticate(created.plaintext)
    assert principal.scopes == {Scope.RUNS_READ}


async def test_last_used_is_written_at_most_once_a_minute(
    service: ApiKeyService, uow: InMemoryUnitOfWork, clock: FrozenClock
) -> None:
    user = await make_user(uow)
    created = await service.create(
        Principal.for_user(user.id, user.role), name="k", scopes=[Scope.RUNS_READ]
    )
    commits_after_create = uow.commits
    await service.authenticate(created.plaintext)
    await service.authenticate(created.plaintext)
    assert uow.commits == commits_after_create + 1
    clock.advance(seconds=61)
    await service.authenticate(created.plaintext)
    assert uow.commits == commits_after_create + 2
    assert created.api_key.last_used_at == clock.now()


async def test_users_cannot_revoke_each_others_keys(
    service: ApiKeyService, uow: InMemoryUnitOfWork
) -> None:
    alice, bob = await make_user(uow), await make_user(uow)
    created = await service.create(
        Principal.for_user(alice.id, alice.role), name="k", scopes=[Scope.RUNS_READ]
    )
    with pytest.raises(NotFound):
        await service.revoke(Principal.for_user(bob.id, bob.role), created.api_key.id)
    admin = await make_user(uow, UserRole.ADMIN)
    await service.revoke(Principal.for_user(admin.id, admin.role), created.api_key.id)
    assert created.api_key.revoked_at is not None


async def test_expiry_is_computed_from_the_clock(
    service: ApiKeyService, uow: InMemoryUnitOfWork, clock: FrozenClock
) -> None:
    user = await make_user(uow)
    created = await service.create(
        Principal.for_user(user.id, user.role),
        name="k",
        scopes=[Scope.RUNS_READ],
        expires_in_days=30,
    )
    assert created.api_key.expires_at == clock.now() + timedelta(days=30)
