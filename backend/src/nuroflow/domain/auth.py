"""Authorization model: roles grant scopes; API keys narrow them.

One permission check, ``principal.require(scope)``, works for both JWT users and API
keys. Endpoints declare *what* they need (a scope), never *who* may call them
(a role). Adding a role later means editing one mapping, not N routers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from nuroflow.domain.enums import UserRole


class Scope(StrEnum):
    WORKFLOWS_READ = "workflows:read"
    WORKFLOWS_WRITE = "workflows:write"
    RUNS_READ = "runs:read"
    RUNS_WRITE = "runs:write"
    EVENTS_WRITE = "events:write"
    # Separate from workflows:write: managing credentials is a more sensitive privilege
    # than editing a workflow, and an automation API key rarely needs it.
    CONNECTIONS_WRITE = "connections:write"
    USERS_ADMIN = "users:admin"


ROLE_SCOPES: dict[UserRole, frozenset[Scope]] = {
    UserRole.VIEWER: frozenset({Scope.WORKFLOWS_READ, Scope.RUNS_READ}),
    UserRole.EDITOR: frozenset(
        {
            Scope.WORKFLOWS_READ,
            Scope.WORKFLOWS_WRITE,
            Scope.RUNS_READ,
            Scope.RUNS_WRITE,
            Scope.EVENTS_WRITE,
            Scope.CONNECTIONS_WRITE,
        }
    ),
    UserRole.ADMIN: frozenset(Scope),
}

# Scopes an API key may never hold, even when its owner is an admin: user and key
# management stays behind interactive (JWT) login.
API_KEY_FORBIDDEN_SCOPES: frozenset[Scope] = frozenset({Scope.USERS_ADMIN})


class AuthMethod(StrEnum):
    JWT = "jwt"
    API_KEY = "api_key"


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller, resolved once per request."""

    user_id: uuid.UUID
    role: UserRole
    method: AuthMethod
    scopes: frozenset[Scope]
    api_key_id: uuid.UUID | None = field(default=None)

    @classmethod
    def for_user(cls, user_id: uuid.UUID, role: UserRole) -> Principal:
        return cls(user_id=user_id, role=role, method=AuthMethod.JWT, scopes=ROLE_SCOPES[role])

    @classmethod
    def for_api_key(
        cls, user_id: uuid.UUID, role: UserRole, key_id: uuid.UUID, key_scopes: frozenset[Scope]
    ) -> Principal:
        # Intersect with the owner's *current* role: demoting a user instantly
        # narrows every key they own, with no key rotation needed.
        effective = key_scopes & ROLE_SCOPES[role] - API_KEY_FORBIDDEN_SCOPES
        return cls(
            user_id=user_id,
            role=role,
            method=AuthMethod.API_KEY,
            scopes=effective,
            api_key_id=key_id,
        )

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN

    def has(self, scope: Scope) -> bool:
        return scope in self.scopes

    def can_access_owned_by(self, owner_id: uuid.UUID) -> bool:
        """Ownership rule shared by every resource: owners, plus admins via JWT."""
        return owner_id == self.user_id or (self.is_admin and self.method is AuthMethod.JWT)
