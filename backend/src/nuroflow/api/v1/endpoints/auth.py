"""/auth: registration, login, token refresh, logout, current principal."""

from __future__ import annotations

from fastapi import APIRouter, status

from nuroflow.api.deps import AuthServiceDep, ClientInfoDep, Container, CurrentPrincipal, UoW
from nuroflow.api.v1.schemas.auth import (
    AuthContext,
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    MeResponse,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserRead,
)
from nuroflow.core.exceptions import AuthenticationError, RateLimited
from nuroflow.services.auth import TokenPair, normalize_email

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_response(pair: TokenPair, container: Container) -> TokenResponse:
    return TokenResponse(
        access_token=pair.access_token,
        expires_in=container.settings.access_token_ttl_seconds,
        access_token_expires_at=pair.access_token_expires_at,
        refresh_token=pair.refresh_token,
        refresh_token_expires_at=pair.refresh_token_expires_at,
    )


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=UserRead)
async def register(body: RegisterRequest, auth: AuthServiceDep) -> UserRead:
    user = await auth.register(email=body.email, password=body.password, full_name=body.full_name)
    return UserRead.model_validate(user)


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, auth: AuthServiceDep, client: ClientInfoDep, container: Container
) -> TokenResponse:
    """Exchange email + password for an access token (15 min) and a refresh token (14 days).

    Throttled per (IP, email) and per IP, which defeats both targeted brute force and
    credential stuffing across many accounts from one source.
    """
    s = container.settings
    ip = client.ip_address or "unknown"
    for key, limit in (
        (f"login:id:{ip}:{normalize_email(body.email)}", s.login_max_attempts_per_identity),
        (f"login:ip:{ip}", s.login_max_attempts_per_ip),
    ):
        result = await container.rate_limiter.hit(
            key, limit=limit, window_seconds=s.login_window_seconds
        )
        if not result.allowed:
            raise RateLimited(
                "Too many login attempts. Try again later.",
                retry_after=result.retry_after_seconds,
            )
    pair = await auth.login(email=body.email, password=body.password, client=client)
    return _token_response(pair, container)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest, auth: AuthServiceDep, client: ClientInfoDep, container: Container
) -> TokenResponse:
    """Rotate the refresh token. The old one stops working immediately; replaying it
    revokes the whole session."""
    pair = await auth.refresh(refresh_token=body.refresh_token, client=client)
    return _token_response(pair, container)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: LogoutRequest, auth: AuthServiceDep) -> None:
    """End this session. Idempotent: unknown or already-revoked tokens also return 204."""
    await auth.logout(refresh_token=body.refresh_token)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(principal: CurrentPrincipal, auth: AuthServiceDep) -> None:
    """End every session for the current user on every device."""
    await auth.logout_everywhere(principal)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest, principal: CurrentPrincipal, auth: AuthServiceDep
) -> None:
    """Change password and revoke all refresh tokens (every device must log in again)."""
    await auth.change_password(
        principal, current_password=body.current_password, new_password=body.new_password
    )


@router.get("/me", response_model=MeResponse)
async def me(principal: CurrentPrincipal, uow: UoW) -> MeResponse:
    user = await uow.users.get(principal.user_id)
    if user is None:  # deleted between authentication and here
        raise AuthenticationError("Account no longer exists")
    return MeResponse(
        user=UserRead.model_validate(user),
        auth=AuthContext(
            method=principal.method,
            scopes=sorted(principal.scopes),
            api_key_id=principal.api_key_id,
        ),
    )
