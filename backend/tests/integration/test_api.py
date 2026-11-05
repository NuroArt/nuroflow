"""HTTP-level tests: real FastAPI app, real Postgres, real Redis (readiness).

These exercise the full stack: routing, validation, dependency injection, services,
SQL, error mapping, headers. Service *rules* are covered exhaustively by the fast
unit tests; here we prove the wiring.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from nuroflow.api.container import AppContainer
from nuroflow.core.rate_limit import InMemoryRateLimiter
from nuroflow.domain.enums import UserRole
from nuroflow.repositories.sqlalchemy import SqlAlchemyUnitOfWork
from nuroflow.services.auth import AuthService

pytestmark = pytest.mark.integration

API = "/api/v1"
PASSWORD = "correct-horse-battery-staple"


# --------------------------------------------------------------------------- helpers
async def create_user(
    container: AppContainer, email: str, role: UserRole = UserRole.EDITOR
) -> uuid.UUID:
    async with container.session_factory() as session:
        auth = AuthService(
            SqlAlchemyUnitOfWork(session),
            hasher=container.password_hasher,
            jwt=container.jwt,
            clock=container.clock,
            policy=container.auth_policy,
        )
        user = await auth.register(email=email, password=PASSWORD, role=role)
        return user.id


async def login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> dict[str, str]:
    response = await client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    tokens: dict[str, str] = response.json()
    return tokens


def bearer(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def assert_problem(response: httpx.Response, status: int, code: str) -> dict[str, object]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, object] = response.json()
    assert body["code"] == code
    assert body["status"] == status
    assert body["request_id"] == response.headers["x-request-id"]
    return body


# --------------------------------------------------------------------------- platform
async def test_health_and_readiness(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json()["status"] == "ok"
    ready = await client.get("/readyz")
    assert ready.status_code == 200
    assert {name: c["status"] for name, c in ready.json()["checks"].items()} == {
        "database": "ok",
        "redis": "ok",
    }


async def test_security_headers_and_request_id_propagation(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "trace-abc-12345"})
    assert response.headers["x-request-id"] == "trace-abc-12345"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]


async def test_malicious_request_id_is_replaced(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "bad id\r\ninjected"})
    assert response.headers["x-request-id"] != "bad id\r\ninjected"
    assert len(response.headers["x-request-id"]) == 32


async def test_unknown_route_is_problem_json(client: httpx.AsyncClient) -> None:
    assert_problem(await client.get(f"{API}/nope"), 404, "not_found")


async def test_openapi_documents_both_auth_schemes(client: httpx.AsyncClient) -> None:
    schemes = (await client.get("/openapi.json")).json()["components"]["securitySchemes"]
    assert {"HTTPBearer", "APIKeyHeader"} <= set(schemes)


# --------------------------------------------------------------------------- auth flows
async def test_register_login_me(client: httpx.AsyncClient) -> None:
    created = await client.post(
        f"{API}/auth/register",
        json={"email": "Jon@NuroWorks.com", "password": PASSWORD, "full_name": "Jon"},
    )
    assert created.status_code == 201
    assert created.json()["email"] == "jon@nuroworks.com"
    assert "hashed_password" not in created.json()

    tokens = await login(client, "jon@nuroworks.com")
    assert tokens["token_type"] == "bearer"
    me = (await client.get(f"{API}/auth/me", headers=bearer(tokens))).json()
    assert me["user"]["role"] == "editor"
    assert me["auth"]["method"] == "jwt"
    assert "users:admin" not in me["auth"]["scopes"]


async def test_validation_errors_never_echo_input(client: httpx.AsyncClient) -> None:
    response = await client.post(
        f"{API}/auth/login", json={"email": "not-an-email", "password": "hunter2-secret"}
    )
    body = assert_problem(response, 422, "validation_error")
    assert "hunter2-secret" not in response.text
    assert body["errors"]


async def test_wrong_password_is_401_with_www_authenticate(
    client: httpx.AsyncClient, container: AppContainer
) -> None:
    await create_user(container, "jon@nuroworks.com")
    response = await client.post(
        f"{API}/auth/login", json={"email": "jon@nuroworks.com", "password": "wrong-password!!"}
    )
    assert_problem(response, 401, "invalid_credentials")
    assert response.headers["www-authenticate"].startswith("Bearer")


async def test_protected_endpoint_requires_credentials(client: httpx.AsyncClient) -> None:
    assert_problem(await client.get(f"{API}/auth/me"), 401, "authentication_failed")
    garbage = {"Authorization": "Bearer not.a.jwt"}
    assert_problem(
        await client.get(f"{API}/auth/me", headers=garbage), 401, "authentication_failed"
    )


async def test_refresh_rotation_and_reuse_detection(
    client: httpx.AsyncClient, container: AppContainer
) -> None:
    await create_user(container, "jon@nuroworks.com")
    original = await login(client, "jon@nuroworks.com")

    rotated = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": original["refresh_token"]}
    )
    assert rotated.status_code == 200
    new_tokens = rotated.json()
    assert new_tokens["refresh_token"] != original["refresh_token"]

    replay = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": original["refresh_token"]}
    )
    assert_problem(replay, 401, "token_reuse_detected")
    # The whole family is dead, including the legitimately rotated token.
    after = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": new_tokens["refresh_token"]}
    )
    assert_problem(after, 401, "authentication_failed")


async def test_logout_then_refresh_fails(
    client: httpx.AsyncClient, container: AppContainer
) -> None:
    await create_user(container, "jon@nuroworks.com")
    tokens = await login(client, "jon@nuroworks.com")
    out = await client.post(f"{API}/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert out.status_code == 204
    again = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert again.status_code == 401


async def test_change_password_ends_other_sessions(
    client: httpx.AsyncClient, container: AppContainer
) -> None:
    await create_user(container, "jon@nuroworks.com")
    tokens = await login(client, "jon@nuroworks.com")
    changed = await client.post(
        f"{API}/auth/change-password",
        headers=bearer(tokens),
        json={"current_password": PASSWORD, "new_password": "brand-new-password-42"},
    )
    assert changed.status_code == 204
    stale = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert stale.status_code == 401
    await login(client, "jon@nuroworks.com", "brand-new-password-42")


async def test_login_is_rate_limited_with_retry_after(
    client: httpx.AsyncClient, container: AppContainer, rate_limiter: InMemoryRateLimiter
) -> None:
    await create_user(container, "jon@nuroworks.com")
    attempt = {"email": "jon@nuroworks.com", "password": "wrong-password!!"}
    for _ in range(container.settings.login_max_attempts_per_identity):
        assert (await client.post(f"{API}/auth/login", json=attempt)).status_code == 401
    blocked = await client.post(f"{API}/auth/login", json=attempt)
    assert_problem(blocked, 429, "rate_limited")
    assert int(blocked.headers["retry-after"]) > 0
    # Even the correct password is refused while blocked: no oracle for the attacker.
    attempt["password"] = PASSWORD
    assert (await client.post(f"{API}/auth/login", json=attempt)).status_code == 429


# --------------------------------------------------------------------------- RBAC
async def test_users_admin_endpoints_require_admin(
    client: httpx.AsyncClient, container: AppContainer
) -> None:
    await create_user(container, "editor@nuroworks.com")
    await create_user(container, "admin@nuroworks.com", UserRole.ADMIN)
    editor = await login(client, "editor@nuroworks.com")
    admin = await login(client, "admin@nuroworks.com")

    denied = await client.get(f"{API}/users", headers=bearer(editor))
    body = assert_problem(denied, 403, "permission_denied")
    assert body["scope"] == "users:admin"

    listed = await client.get(f"{API}/users", headers=bearer(admin))
    assert listed.status_code == 200
    assert {u["email"] for u in listed.json()["items"]} == {
        "editor@nuroworks.com",
        "admin@nuroworks.com",
    }


async def test_user_list_cursor_pagination(
    client: httpx.AsyncClient, container: AppContainer
) -> None:
    await create_user(container, "admin@nuroworks.com", UserRole.ADMIN)
    for i in range(4):
        await create_user(container, f"user{i}@nuroworks.com")
    headers = bearer(await login(client, "admin@nuroworks.com"))

    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = (await client.get(f"{API}/users", headers=headers, params=params)).json()
        seen += [u["email"] for u in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 5
    assert len(set(seen)) == 5
    assert seen[0] == "user3@nuroworks.com"  # newest first (UUIDv7 order)


async def test_deactivated_user_loses_access_immediately(
    client: httpx.AsyncClient, container: AppContainer
) -> None:
    victim_id = await create_user(container, "editor@nuroworks.com")
    await create_user(container, "admin@nuroworks.com", UserRole.ADMIN)
    victim = await login(client, "editor@nuroworks.com")
    admin = await login(client, "admin@nuroworks.com")

    patched = await client.patch(
        f"{API}/users/{victim_id}", headers=bearer(admin), json={"is_active": False}
    )
    assert patched.status_code == 200
    # The 15-minute access token still has time left, but it's refused now.
    assert (await client.get(f"{API}/auth/me", headers=bearer(victim))).status_code == 401


async def test_admin_cannot_demote_themselves(
    client: httpx.AsyncClient, container: AppContainer
) -> None:
    admin_id = await create_user(container, "admin@nuroworks.com", UserRole.ADMIN)
    admin = await login(client, "admin@nuroworks.com")
    response = await client.patch(
        f"{API}/users/{admin_id}", headers=bearer(admin), json={"role": "viewer"}
    )
    assert_problem(response, 422, "validation_error")


# --------------------------------------------------------------------------- API keys
async def test_api_key_lifecycle(client: httpx.AsyncClient, container: AppContainer) -> None:
    await create_user(container, "jon@nuroworks.com")
    headers = bearer(await login(client, "jon@nuroworks.com"))

    created = await client.post(
        f"{API}/api-keys",
        headers=headers,
        json={"name": "lead-form", "scopes": ["events:write"], "expires_in_days": 30},
    )
    assert created.status_code == 201
    key = created.json()
    assert key["key"].startswith(f"nf_{key['prefix']}_")
    assert key["hint"] == f"nf_{key['prefix']}_…"

    as_key = {"X-API-Key": key["key"]}
    me = (await client.get(f"{API}/auth/me", headers=as_key)).json()
    assert me["auth"] == {"method": "api_key", "scopes": ["events:write"], "api_key_id": key["id"]}

    listed = (await client.get(f"{API}/api-keys", headers=headers)).json()
    assert [k["id"] for k in listed] == [key["id"]]
    assert "key" not in listed[0]  # the secret is never shown again

    # Keys can't manage keys, and can't reach admin routes.
    assert (await client.get(f"{API}/api-keys", headers=as_key)).status_code == 200
    minted = await client.post(
        f"{API}/api-keys", headers=as_key, json={"name": "x", "scopes": ["events:write"]}
    )
    assert_problem(minted, 403, "permission_denied")

    assert (await client.delete(f"{API}/api-keys/{key['id']}", headers=headers)).status_code == 204
    assert (await client.get(f"{API}/auth/me", headers=as_key)).status_code == 401


async def test_both_credentials_at_once_is_rejected(
    client: httpx.AsyncClient, container: AppContainer
) -> None:
    await create_user(container, "jon@nuroworks.com")
    tokens = await login(client, "jon@nuroworks.com")
    headers = bearer(tokens) | {"X-API-Key": "nf_abcdefghijkl_" + "a" * 43}
    assert_problem(
        await client.get(f"{API}/auth/me", headers=headers), 401, "authentication_failed"
    )


async def test_registration_conflict_is_409(
    client: httpx.AsyncClient, container: AppContainer
) -> None:
    await create_user(container, "jon@nuroworks.com")
    response = await client.post(
        f"{API}/auth/register", json={"email": "JON@nuroworks.com", "password": PASSWORD}
    )
    body = assert_problem(response, 409, "conflict")
    assert body["field"] == "email"
