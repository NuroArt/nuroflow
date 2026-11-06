"""Authentication use cases: register, login, refresh rotation, logout, password change.

Refresh-token rotation with reuse detection
===========================================

Every refresh returns a *new* refresh token and revokes the old one, linking them with
``replaced_by_id`` inside one ``family_id``. If a token that was already rotated is
presented again, either the legitimate client or an attacker is replaying a stolen
copy. We can't tell which, so the safe response is to revoke the **whole family** and
force a fresh login. (Pattern from OAuth 2.0 Security BCP, RFC 9700 §4.14.)

The lookup uses ``SELECT … FOR UPDATE``, so two concurrent refreshes of the same
token serialise: one wins, and the other sees a rotated token. That's strict on
purpose. A client with parallel tabs should share one refresh in flight. The
alternative (a short reuse grace period) is documented in docs/AUTH.md.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import EmailStr, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from nuroflow.core.clock import Clock
from nuroflow.core.exceptions import (
    AuthenticationError,
    Conflict,
    FeatureDisabled,
    InvalidCredentials,
    TokenExpired,
    TokenReuseDetected,
    ValidationError,
)
from nuroflow.core.ids import uuid7
from nuroflow.core.security import (
    JWTService,
    PasswordHasher,
    generate_refresh_token,
    hash_refresh_token,
)
from nuroflow.db.models import RefreshToken, User
from nuroflow.domain.auth import Principal
from nuroflow.domain.enums import UserRole
from nuroflow.repositories.interfaces import UnitOfWork

logger = logging.getLogger(__name__)

PASSWORD_MAX_LENGTH = 128  # bounds Argon2 CPU per request (long-password DoS)


@dataclass(frozen=True, slots=True)
class AuthPolicy:
    refresh_token_ttl: timedelta
    registration_enabled: bool
    password_min_length: int


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    access_token_expires_at: datetime
    refresh_token: str
    refresh_token_expires_at: datetime
    token_type: str = "bearer"  # noqa: S105 - OAuth token type, not a secret


@dataclass(frozen=True, slots=True)
class ClientInfo:
    user_agent: str | None = None
    ip_address: str | None = None


def normalize_email(email: str) -> str:
    return email.strip().lower()


_EMAIL = TypeAdapter(EmailStr)


def validate_email(email: str) -> str:
    """Apply the same rules as the API's ``EmailStr`` schemas.

    The service validates too, because not every caller goes through the HTTP schema:
    ``nuroflow create-admin`` once created an admin with a reserved-domain address
    (``admin@x.local``) that the login endpoint then rejected, locking the admin out.
    """
    try:
        return normalize_email(_EMAIL.validate_python(email.strip()))
    except PydanticValidationError as exc:
        reason = str(exc.errors()[0].get("msg", "invalid email address"))
        raise ValidationError(f"Invalid email address: {reason}", field="email") from None


class AuthService:
    def __init__(
        self,
        uow: UnitOfWork,
        *,
        hasher: PasswordHasher,
        jwt: JWTService,
        clock: Clock,
        policy: AuthPolicy,
    ) -> None:
        self._uow = uow
        self._hasher = hasher
        self._jwt = jwt
        self._clock = clock
        self._policy = policy

    # ------------------------------------------------------------------ #
    async def register(
        self,
        *,
        email: str,
        password: str,
        full_name: str | None = None,
        role: UserRole = UserRole.EDITOR,
        bypass_registration_policy: bool = False,
    ) -> User:
        if not (self._policy.registration_enabled or bypass_registration_policy):
            raise FeatureDisabled("Self-service registration is disabled")
        email = validate_email(email)
        self._validate_password(password, email=email)
        if await self._uow.users.get_by_email(email) is not None:
            raise Conflict("An account with this email already exists", field="email")

        user = User(
            id=uuid7(),
            email=email,
            hashed_password=self._hasher.hash(password),
            full_name=full_name,
            role=role,
            is_active=True,
        )
        await self._uow.users.add(user)
        await self._uow.commit()
        logger.info("user registered", extra={"user_id": str(user.id), "role": role.value})
        return user

    async def login(self, *, email: str, password: str, client: ClientInfo) -> TokenPair:
        user = await self._uow.users.get_by_email(normalize_email(email))
        # Always verify, even for unknown users, so response timing can't reveal
        # which emails have accounts.
        password_ok = self._hasher.verify(user.hashed_password if user else None, password)
        if user is None or not password_ok or not user.is_active:
            raise InvalidCredentials()

        now = self._clock.now()
        if self._hasher.needs_rehash(user.hashed_password):
            user.hashed_password = self._hasher.hash(password)  # transparent upgrade
        user.last_login_at = now
        pair, _ = await self._issue_pair(user, family_id=uuid7(), client=client, now=now)
        await self._uow.commit()
        logger.info("login succeeded", extra={"user_id": str(user.id)})
        return pair

    async def refresh(self, *, refresh_token: str, client: ClientInfo) -> TokenPair:
        now = self._clock.now()
        stored = await self._uow.refresh_tokens.get_by_hash_for_update(
            hash_refresh_token(refresh_token)
        )
        if stored is None:
            raise AuthenticationError("Invalid refresh token")

        if stored.revoked_at is not None:
            if stored.replaced_by_id is not None:
                revoked = await self._uow.refresh_tokens.revoke_family(stored.family_id, at=now)
                await self._uow.commit()
                logger.warning(
                    "refresh token reuse detected; family revoked",
                    extra={
                        "user_id": str(stored.user_id),
                        "family_id": str(stored.family_id),
                        "revoked": revoked,
                        "ip": client.ip_address,
                    },
                )
                raise TokenReuseDetected()
            raise AuthenticationError("Refresh token has been revoked")

        if stored.expires_at <= now:
            raise TokenExpired("Refresh token has expired")

        user = await self._uow.users.get(stored.user_id)
        if user is None or not user.is_active:
            await self._uow.refresh_tokens.revoke_family(stored.family_id, at=now)
            await self._uow.commit()
            raise AuthenticationError("Account is not active")

        pair, successor = await self._issue_pair(
            user, family_id=stored.family_id, client=client, now=now
        )
        stored.revoked_at = now
        stored.replaced_by_id = successor.id
        await self._uow.commit()
        return pair

    async def logout(self, *, refresh_token: str) -> None:
        """Revoke the session (token family). Idempotent and silent for unknown tokens."""
        stored = await self._uow.refresh_tokens.get_by_hash_for_update(
            hash_refresh_token(refresh_token)
        )
        if stored is not None:
            await self._uow.refresh_tokens.revoke_family(stored.family_id, at=self._clock.now())
            await self._uow.commit()

    async def logout_everywhere(self, principal: Principal) -> int:
        revoked = await self._uow.refresh_tokens.revoke_all_for_user(
            principal.user_id, at=self._clock.now()
        )
        await self._uow.commit()
        return revoked

    async def change_password(
        self, principal: Principal, *, current_password: str, new_password: str
    ) -> None:
        user = await self._uow.users.get(principal.user_id)
        if user is None or not self._hasher.verify(user.hashed_password, current_password):
            raise InvalidCredentials("Current password is incorrect")
        self._validate_password(new_password, email=user.email)
        user.hashed_password = self._hasher.hash(new_password)
        # A password change is often a response to a suspected compromise: end every session.
        await self._uow.refresh_tokens.revoke_all_for_user(user.id, at=self._clock.now())
        await self._uow.commit()

    async def authenticate_access_token(self, token: str) -> Principal:
        """Verify a JWT and re-check the user against the DB.

        The extra primary-key lookup per request is deliberate: deactivating a user or
        changing their role takes effect immediately, not after the token expires.
        """
        claims = self._jwt.verify(token, now=self._clock.now())
        user = await self._uow.users.get(claims.user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("Account is not active")
        return Principal.for_user(user.id, user.role)

    # ------------------------------------------------------------------ #
    async def _issue_pair(
        self, user: User, *, family_id: uuid.UUID, client: ClientInfo, now: datetime
    ) -> tuple[TokenPair, RefreshToken]:
        access_token, access_exp = self._jwt.issue(user_id=user.id, role=user.role, now=now)
        raw_refresh = generate_refresh_token()
        refresh_exp = now + self._policy.refresh_token_ttl
        record = RefreshToken(
            id=uuid7(),
            user_id=user.id,
            family_id=family_id,
            token_hash=hash_refresh_token(raw_refresh),
            expires_at=refresh_exp,
            user_agent=(client.user_agent or "")[:512] or None,
            ip_address=client.ip_address,
        )
        await self._uow.refresh_tokens.add(record)
        pair = TokenPair(
            access_token=access_token,
            access_token_expires_at=access_exp,
            refresh_token=raw_refresh,
            refresh_token_expires_at=refresh_exp,
        )
        return pair, record

    def _validate_password(self, password: str, *, email: str) -> None:
        if len(password) < self._policy.password_min_length:
            raise ValidationError(
                f"Password must be at least {self._policy.password_min_length} characters",
                field="password",
            )
        if len(password) > PASSWORD_MAX_LENGTH:
            raise ValidationError(
                f"Password must be at most {PASSWORD_MAX_LENGTH} characters", field="password"
            )
        if password.strip().lower() == email.lower():
            raise ValidationError("Password must not be your email address", field="password")
