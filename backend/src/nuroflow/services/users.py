"""User administration use cases (admin only)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from nuroflow.core.clock import Clock
from nuroflow.core.exceptions import NotFound, PermissionDenied, ValidationError
from nuroflow.db.models import User
from nuroflow.domain.auth import Principal, Scope
from nuroflow.domain.enums import UserRole
from nuroflow.repositories.interfaces import UnitOfWork

logger = logging.getLogger(__name__)


class UserService:
    def __init__(self, uow: UnitOfWork, *, clock: Clock) -> None:
        self._uow = uow
        self._clock = clock

    async def get(self, user_id: uuid.UUID) -> User:
        user = await self._uow.users.get(user_id)
        if user is None:
            raise NotFound("User not found")
        return user

    async def list(
        self, principal: Principal, *, limit: int, before_id: uuid.UUID | None
    ) -> Sequence[User]:
        self._require_admin(principal)
        return await self._uow.users.list_page(limit=limit, before_id=before_id)

    async def update(
        self,
        principal: Principal,
        user_id: uuid.UUID,
        *,
        role: UserRole | None = None,
        is_active: bool | None = None,
    ) -> User:
        self._require_admin(principal)
        if user_id == principal.user_id and (
            (role is not None and role is not UserRole.ADMIN) or is_active is False
        ):
            # Guard rail: an admin can't lock themselves (and possibly everyone) out.
            raise ValidationError("You cannot demote or deactivate your own account")

        user = await self.get(user_id)
        if role is not None:
            user.role = role
        if is_active is not None:
            user.is_active = is_active
            if not is_active:
                await self._uow.refresh_tokens.revoke_all_for_user(user.id, at=self._clock.now())
        await self._uow.commit()
        logger.info(
            "user updated",
            extra={
                "actor": str(principal.user_id),
                "user_id": str(user_id),
                "role": role,
                "is_active": is_active,
            },
        )
        return user

    @staticmethod
    def _require_admin(principal: Principal) -> None:
        if not principal.has(Scope.USERS_ADMIN):
            raise PermissionDenied("Administrator access required")
