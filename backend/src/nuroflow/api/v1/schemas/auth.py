from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import EmailStr, Field

from nuroflow.api.v1.schemas.common import Schema
from nuroflow.domain.auth import AuthMethod, Scope
from nuroflow.domain.enums import UserRole

# The service enforces the real password policy (min length from settings). The schema
# only bounds the size, so a 1 MB "password" never reaches Argon2.
_Password = Field(min_length=1, max_length=256)


class RegisterRequest(Schema):
    email: EmailStr
    password: str = _Password
    full_name: str | None = Field(default=None, max_length=200)


class LoginRequest(Schema):
    email: EmailStr
    password: str = _Password


class RefreshRequest(Schema):
    refresh_token: str = Field(min_length=20, max_length=200)


class LogoutRequest(RefreshRequest):
    pass


class ChangePasswordRequest(Schema):
    current_password: str = _Password
    new_password: str = _Password


class TokenResponse(Schema):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth token type, not a secret
    expires_in: int = Field(description="Access token lifetime in seconds")
    access_token_expires_at: datetime
    refresh_token: str
    refresh_token_expires_at: datetime


class UserRead(Schema):
    id: uuid.UUID
    email: str
    full_name: str | None
    role: UserRole
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class AuthContext(Schema):
    method: AuthMethod
    scopes: list[Scope]
    api_key_id: uuid.UUID | None


class MeResponse(Schema):
    user: UserRead
    auth: AuthContext
