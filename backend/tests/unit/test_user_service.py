from __future__ import annotations

import pytest

from nuroflow.core.clock import FrozenClock
from nuroflow.core.exceptions import NotFound, PermissionDenied, ValidationError
from nuroflow.core.ids import uuid7
from nuroflow.db.models import RefreshToken, User
from nuroflow.domain.auth import Principal
from nuroflow.domain.enums import UserRole
from nuroflow.services.users import UserService
from tests.fakes import InMemoryUnitOfWork


@pytest.fixture
def uow() -> InMemoryUnitOfWork:
    return InMemoryUnitOfWork()


@pytest.fixture
def service(uow: InMemoryUnitOfWork) -> UserService:
    return UserService(uow, clock=FrozenClock())


async def add_user(uow: InMemoryUnitOfWork, role: UserRole) -> User:
    user = User(
        id=uuid7(), email=f"{uuid7().hex}@x.io", hashed_password="h", role=role, is_active=True
    )
    await uow.users.add(user)
    return user


def principal(user: User) -> Principal:
    return Principal.for_user(user.id, user.role)


async def test_non_admin_cannot_list_or_update(
    service: UserService, uow: InMemoryUnitOfWork
) -> None:
    editor = await add_user(uow, UserRole.EDITOR)
    with pytest.raises(PermissionDenied):
        await service.list(principal(editor), limit=10, before_id=None)
    with pytest.raises(PermissionDenied):
        await service.update(principal(editor), editor.id, role=UserRole.ADMIN)


async def test_admin_updates_role(service: UserService, uow: InMemoryUnitOfWork) -> None:
    admin = await add_user(uow, UserRole.ADMIN)
    editor = await add_user(uow, UserRole.EDITOR)
    updated = await service.update(principal(admin), editor.id, role=UserRole.VIEWER)
    assert updated.role is UserRole.VIEWER


async def test_deactivation_revokes_sessions(service: UserService, uow: InMemoryUnitOfWork) -> None:
    admin = await add_user(uow, UserRole.ADMIN)
    editor = await add_user(uow, UserRole.EDITOR)
    token = RefreshToken(
        id=uuid7(),
        user_id=editor.id,
        family_id=uuid7(),
        token_hash="h" * 64,
        expires_at=FrozenClock().now(),
    )
    await uow.refresh_tokens.add(token)
    await service.update(principal(admin), editor.id, is_active=False)
    assert editor.is_active is False
    assert token.revoked_at is not None


@pytest.mark.parametrize("change", [{"role": UserRole.VIEWER}, {"is_active": False}])
async def test_admin_cannot_lock_themselves_out(
    service: UserService, uow: InMemoryUnitOfWork, change: dict[str, object]
) -> None:
    admin = await add_user(uow, UserRole.ADMIN)
    with pytest.raises(ValidationError, match="your own account"):
        await service.update(principal(admin), admin.id, **change)  # type: ignore[arg-type]


async def test_update_unknown_user_is_404(service: UserService, uow: InMemoryUnitOfWork) -> None:
    admin = await add_user(uow, UserRole.ADMIN)
    with pytest.raises(NotFound):
        await service.update(principal(admin), uuid7(), role=UserRole.VIEWER)
