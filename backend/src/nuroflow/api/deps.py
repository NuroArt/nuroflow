"""FastAPI dependencies: the wiring between HTTP and the application layer.

Dependency graph for an authenticated request::

    get_container ─▶ get_session ─▶ get_uow ─┬▶ get_auth_service ────┐
                                             └▶ get_api_key_service ─┴▶ get_principal
                                                                         └▶ require_scope(...)

Routers only ever see ``Principal`` and services, never sessions or tokens.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from nuroflow.api.container import AppContainer
from nuroflow.core.exceptions import AuthenticationError, PermissionDenied
from nuroflow.domain.auth import Principal, Scope
from nuroflow.repositories.sqlalchemy import SqlAlchemyUnitOfWork
from nuroflow.services.api_keys import ApiKeyService
from nuroflow.services.auth import AuthService, ClientInfo
from nuroflow.services.users import UserService

bearer_scheme = HTTPBearer(auto_error=False, description="Access token from /auth/login")
api_key_scheme = APIKeyHeader(
    name="X-API-Key", auto_error=False, description="API key (nf_<prefix>_<secret>)"
)


def get_container(request: Request) -> AppContainer:
    container: AppContainer = request.app.state.container
    return container


Container = Annotated[AppContainer, Depends(get_container)]


async def get_session(container: Container) -> AsyncIterator[AsyncSession]:
    """One session per request. Services commit explicitly; anything uncommitted is
    rolled back when the session closes, including on exceptions."""
    async with container.session_factory() as session:
        yield session


async def get_uow(session: Annotated[AsyncSession, Depends(get_session)]) -> SqlAlchemyUnitOfWork:
    return SqlAlchemyUnitOfWork(session)


UoW = Annotated[SqlAlchemyUnitOfWork, Depends(get_uow)]


def get_auth_service(uow: UoW, container: Container) -> AuthService:
    return AuthService(
        uow,
        hasher=container.password_hasher,
        jwt=container.jwt,
        clock=container.clock,
        policy=container.auth_policy,
    )


def get_api_key_service(uow: UoW, container: Container) -> ApiKeyService:
    return ApiKeyService(uow, codec=container.api_key_codec, clock=container.clock)


def get_user_service(uow: UoW, container: Container) -> UserService:
    return UserService(uow, clock=container.clock)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
ApiKeyServiceDep = Annotated[ApiKeyService, Depends(get_api_key_service)]
UserServiceDep = Annotated[UserService, Depends(get_user_service)]


def get_client_info(request: Request) -> ClientInfo:
    # Behind a reverse proxy, run uvicorn with --proxy-headers and
    # --forwarded-allow-ips=<proxy IP>, so request.client is the real caller rather
    # than a spoofable X-Forwarded-For value.
    return ClientInfo(
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )


ClientInfoDep = Annotated[ClientInfo, Depends(get_client_info)]


async def get_principal(
    auth: AuthServiceDep,
    api_keys: ApiKeyServiceDep,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    api_key: Annotated[str | None, Depends(api_key_scheme)],
) -> Principal:
    if bearer is not None and api_key is not None:
        raise AuthenticationError("Send either a bearer token or an API key, not both")
    if bearer is not None:
        return await auth.authenticate_access_token(bearer.credentials)
    if api_key is not None:
        return await api_keys.authenticate(api_key)
    raise AuthenticationError("Authentication required")


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def require_scope(scope: Scope) -> Callable[[Principal], Awaitable[Principal]]:
    """Endpoint guard: ``principal: Annotated[Principal, Depends(require_scope(Scope.X))]``."""

    async def _guard(principal: CurrentPrincipal) -> Principal:
        if not principal.has(scope):
            raise PermissionDenied(f"Missing required scope: {scope.value}", scope=scope.value)
        return principal

    return _guard
