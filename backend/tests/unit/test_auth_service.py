"""AuthService against in-memory fakes: every rule, no database, about 1 second."""

from __future__ import annotations

from datetime import timedelta

import pytest

from nuroflow.core.clock import FrozenClock
from nuroflow.core.exceptions import (
    AuthenticationError,
    Conflict,
    FeatureDisabled,
    InvalidCredentials,
    TokenExpired,
    TokenReuseDetected,
    ValidationError,
)
from nuroflow.core.security import JWTService, PasswordHasher
from nuroflow.domain.auth import Principal, Scope
from nuroflow.domain.enums import UserRole
from nuroflow.services.auth import AuthPolicy, AuthService, ClientInfo
from tests.fakes import InMemoryUnitOfWork

PASSWORD = "a-long-enough-password"
CLIENT = ClientInfo(user_agent="pytest", ip_address="127.0.0.1")


@pytest.fixture(scope="module")
def hasher() -> PasswordHasher:
    return PasswordHasher()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def uow() -> InMemoryUnitOfWork:
    return InMemoryUnitOfWork()


def make_service(
    uow: InMemoryUnitOfWork,
    hasher: PasswordHasher,
    clock: FrozenClock,
    *,
    registration_enabled: bool = True,
) -> AuthService:
    jwt = JWTService(
        signing_key=b"k" * 32,
        algorithm="HS256",
        issuer="nuroflow",
        audience="nuroflow-api",
        ttl=timedelta(minutes=15),
        leeway=timedelta(0),
    )
    policy = AuthPolicy(
        refresh_token_ttl=timedelta(days=14),
        registration_enabled=registration_enabled,
        password_min_length=12,
    )
    return AuthService(uow, hasher=hasher, jwt=jwt, clock=clock, policy=policy)


@pytest.fixture
def auth(uow: InMemoryUnitOfWork, hasher: PasswordHasher, clock: FrozenClock) -> AuthService:
    return make_service(uow, hasher, clock)


# --------------------------------------------------------------------------- register
async def test_register_normalises_email_and_hashes_password(
    auth: AuthService, uow: InMemoryUnitOfWork
) -> None:
    user = await auth.register(email="  Jon@NuroWorks.COM ", password=PASSWORD)
    assert user.email == "jon@nuroworks.com"
    assert user.role is UserRole.EDITOR
    assert user.hashed_password != PASSWORD
    assert uow.commits == 1


async def test_register_rejects_duplicate_email_case_insensitively(auth: AuthService) -> None:
    await auth.register(email="jon@nuroworks.com", password=PASSWORD)
    with pytest.raises(Conflict):
        await auth.register(email="JON@nuroworks.com", password=PASSWORD)


@pytest.mark.parametrize(
    ("password", "reason"),
    [("short", "at least 12"), ("x" * 129, "at most 128"), ("jon@nuroworks.com", "email")],
)
async def test_register_enforces_password_policy(
    auth: AuthService, password: str, reason: str
) -> None:
    with pytest.raises(ValidationError, match=reason):
        await auth.register(email="jon@nuroworks.com", password=password)


async def test_register_can_be_disabled(
    uow: InMemoryUnitOfWork, hasher: PasswordHasher, clock: FrozenClock
) -> None:
    service = make_service(uow, hasher, clock, registration_enabled=False)
    with pytest.raises(FeatureDisabled):
        await service.register(email="jon@nuroworks.com", password=PASSWORD)
    # The CLI bootstrap path is still allowed.
    admin = await service.register(
        email="root@nuroworks.com",
        password=PASSWORD,
        role=UserRole.ADMIN,
        bypass_registration_policy=True,
    )
    assert admin.role is UserRole.ADMIN


# --------------------------------------------------------------------------- login
async def test_login_issues_working_token_pair(auth: AuthService, clock: FrozenClock) -> None:
    user = await auth.register(email="jon@nuroworks.com", password=PASSWORD)
    pair = await auth.login(email="JON@nuroworks.com", password=PASSWORD, client=CLIENT)
    principal = await auth.authenticate_access_token(pair.access_token)
    assert principal == Principal.for_user(user.id, UserRole.EDITOR)
    assert user.last_login_at == clock.now()


@pytest.mark.parametrize(
    ("email", "password"),
    [("jon@nuroworks.com", "wrong-password-123"), ("nobody@nuroworks.com", PASSWORD)],
    ids=["wrong-password", "unknown-email"],
)
async def test_login_failures_are_indistinguishable(
    auth: AuthService, email: str, password: str
) -> None:
    await auth.register(email="jon@nuroworks.com", password=PASSWORD)
    with pytest.raises(InvalidCredentials) as exc_info:
        await auth.login(email=email, password=password, client=CLIENT)
    assert exc_info.value.detail == "Invalid email or password"


async def test_inactive_user_cannot_login(auth: AuthService) -> None:
    user = await auth.register(email="jon@nuroworks.com", password=PASSWORD)
    user.is_active = False
    with pytest.raises(InvalidCredentials):
        await auth.login(email=user.email, password=PASSWORD, client=CLIENT)


# --------------------------------------------------------------------------- refresh
async def test_refresh_rotates_and_links_tokens(auth: AuthService, uow: InMemoryUnitOfWork) -> None:
    await auth.register(email="jon@nuroworks.com", password=PASSWORD)
    first = await auth.login(email="jon@nuroworks.com", password=PASSWORD, client=CLIENT)
    second = await auth.refresh(refresh_token=first.refresh_token, client=CLIENT)

    assert second.refresh_token != first.refresh_token
    tokens = list(uow.refresh_tokens.rows.values())
    old, new = tokens
    assert old.revoked_at is not None
    assert old.replaced_by_id == new.id
    assert new.revoked_at is None
    assert new.family_id == old.family_id


async def test_replaying_rotated_token_revokes_entire_family(
    auth: AuthService, uow: InMemoryUnitOfWork
) -> None:
    await auth.register(email="jon@nuroworks.com", password=PASSWORD)
    stolen = await auth.login(email="jon@nuroworks.com", password=PASSWORD, client=CLIENT)
    legit = await auth.refresh(refresh_token=stolen.refresh_token, client=CLIENT)

    with pytest.raises(TokenReuseDetected):
        await auth.refresh(refresh_token=stolen.refresh_token, client=CLIENT)
    # The legitimate client's current token died with the family: forced re-login.
    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token=legit.refresh_token, client=CLIENT)
    assert all(t.revoked_at is not None for t in uow.refresh_tokens.rows.values())


async def test_expired_refresh_token_is_rejected(auth: AuthService, clock: FrozenClock) -> None:
    await auth.register(email="jon@nuroworks.com", password=PASSWORD)
    pair = await auth.login(email="jon@nuroworks.com", password=PASSWORD, client=CLIENT)
    clock.advance(days=15)
    with pytest.raises(TokenExpired):
        await auth.refresh(refresh_token=pair.refresh_token, client=CLIENT)


async def test_refresh_for_deactivated_user_fails(auth: AuthService) -> None:
    user = await auth.register(email="jon@nuroworks.com", password=PASSWORD)
    pair = await auth.login(email=user.email, password=PASSWORD, client=CLIENT)
    user.is_active = False
    with pytest.raises(AuthenticationError, match="not active"):
        await auth.refresh(refresh_token=pair.refresh_token, client=CLIENT)


async def test_unknown_refresh_token_is_rejected(auth: AuthService) -> None:
    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token="x" * 43, client=CLIENT)


# --------------------------------------------------------------------------- logout & password
async def test_logout_revokes_session_and_is_idempotent(auth: AuthService) -> None:
    await auth.register(email="jon@nuroworks.com", password=PASSWORD)
    pair = await auth.login(email="jon@nuroworks.com", password=PASSWORD, client=CLIENT)
    await auth.logout(refresh_token=pair.refresh_token)
    await auth.logout(refresh_token=pair.refresh_token)
    await auth.logout(refresh_token="never-existed-token-value")
    with pytest.raises(AuthenticationError):
        await auth.refresh(refresh_token=pair.refresh_token, client=CLIENT)


async def test_change_password_revokes_every_session(auth: AuthService) -> None:
    user = await auth.register(email="jon@nuroworks.com", password=PASSWORD)
    laptop = await auth.login(email=user.email, password=PASSWORD, client=CLIENT)
    phone = await auth.login(email=user.email, password=PASSWORD, client=CLIENT)
    principal = Principal.for_user(user.id, user.role)

    with pytest.raises(InvalidCredentials):
        await auth.change_password(
            principal, current_password="wrong-password!!", new_password="new-password-456"
        )
    await auth.change_password(
        principal, current_password=PASSWORD, new_password="new-password-456"
    )
    for pair in (laptop, phone):
        with pytest.raises(AuthenticationError):
            await auth.refresh(refresh_token=pair.refresh_token, client=CLIENT)
    await auth.login(email=user.email, password="new-password-456", client=CLIENT)


# --------------------------------------------------------------------------- access tokens
async def test_access_token_stops_working_when_user_deactivated(auth: AuthService) -> None:
    user = await auth.register(email="jon@nuroworks.com", password=PASSWORD)
    pair = await auth.login(email=user.email, password=PASSWORD, client=CLIENT)
    user.is_active = False
    with pytest.raises(AuthenticationError, match="not active"):
        await auth.authenticate_access_token(pair.access_token)


async def test_role_change_applies_to_existing_access_token(auth: AuthService) -> None:
    user = await auth.register(email="jon@nuroworks.com", password=PASSWORD)
    pair = await auth.login(email=user.email, password=PASSWORD, client=CLIENT)
    user.role = UserRole.VIEWER
    principal = await auth.authenticate_access_token(pair.access_token)
    assert not principal.has(Scope.WORKFLOWS_WRITE)


async def test_access_token_expires_on_the_service_clock(
    auth: AuthService, clock: FrozenClock
) -> None:
    user = await auth.register(email="jon@nuroworks.com", password=PASSWORD)
    pair = await auth.login(email=user.email, password=PASSWORD, client=CLIENT)
    clock.advance(minutes=16)
    with pytest.raises(TokenExpired):
        await auth.authenticate_access_token(pair.access_token)


@pytest.mark.parametrize("email", ["admin@nuroflow.local", "not-an-email", "a@b"])
async def test_register_rejects_emails_the_login_schema_would_reject(
    auth: AuthService, email: str
) -> None:
    # Regression: the CLI bypasses HTTP schemas and once created an admin (x@y.local)
    # who could never log in. The service now applies the same EmailStr rules.
    with pytest.raises(ValidationError, match="Invalid email address"):
        await auth.register(
            email=email, password="correct-horse-battery", bypass_registration_policy=True
        )
