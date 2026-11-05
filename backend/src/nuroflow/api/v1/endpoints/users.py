"""/users: administrator user management."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from nuroflow.api.deps import UserServiceDep, require_scope
from nuroflow.api.v1.schemas.auth import UserRead
from nuroflow.api.v1.schemas.common import Page, decode_cursor, encode_cursor
from nuroflow.api.v1.schemas.users import UserUpdate
from nuroflow.domain.auth import Principal, Scope

router = APIRouter(prefix="/users", tags=["users"])

AdminPrincipal = Annotated[Principal, Depends(require_scope(Scope.USERS_ADMIN))]


@router.get("", response_model=Page[UserRead])
async def list_users(
    principal: AdminPrincipal,
    users: UserServiceDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(description="next_cursor from the previous page")] = None,
) -> Page[UserRead]:
    # Fetch one extra row to know whether another page exists, without a COUNT(*).
    rows = await users.list(principal, limit=limit + 1, before_id=decode_cursor(cursor))
    items = [UserRead.model_validate(u) for u in rows[:limit]]
    next_cursor = encode_cursor(items[-1].id) if len(rows) > limit else None
    return Page[UserRead](items=items, next_cursor=next_cursor)


@router.get("/{user_id}", response_model=UserRead)
async def get_user(user_id: uuid.UUID, _: AdminPrincipal, users: UserServiceDep) -> UserRead:
    return UserRead.model_validate(await users.get(user_id))


@router.patch("/{user_id}", response_model=UserRead)
async def update_user(
    user_id: uuid.UUID, body: UserUpdate, principal: AdminPrincipal, users: UserServiceDep
) -> UserRead:
    """Change a user's role or deactivate them. Deactivation revokes all their sessions."""
    user = await users.update(principal, user_id, role=body.role, is_active=body.is_active)
    return UserRead.model_validate(user)
