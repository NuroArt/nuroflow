from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt as pyjwt
import pytest

from nuroflow.core.exceptions import AuthenticationError, TokenExpired
from nuroflow.core.security import (
    ApiKeyCodec,
    JWTService,
    PasswordHasher,
    generate_refresh_token,
    hash_refresh_token,
)
from nuroflow.domain.enums import UserRole

KEY = b"k" * 32
NOW = datetime.now(UTC)


def make_jwt(**overrides: object) -> JWTService:
    params: dict[str, object] = {
        "signing_key": KEY,
        "algorithm": "HS256",
        "issuer": "nuroflow",
        "audience": "nuroflow-api",
        "ttl": timedelta(minutes=15),
    }
    params.update(overrides)
    return JWTService(**params)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- passwords
@pytest.fixture(scope="module")
def hasher() -> PasswordHasher:
    return PasswordHasher()


def test_password_hash_is_argon2id_and_verifies(hasher: PasswordHasher) -> None:
    hashed = hasher.hash("correct horse battery staple")
    assert hashed.startswith("$argon2id$")
    assert hasher.verify(hashed, "correct horse battery staple")
    assert not hasher.verify(hashed, "Correct horse battery staple")


def test_password_verify_against_missing_user_is_false(hasher: PasswordHasher) -> None:
    # Runs a real Argon2 verification (constant-ish time) and still returns False.
    assert hasher.verify(None, "anything") is False


def test_password_verify_tolerates_garbage_hash(hasher: PasswordHasher) -> None:
    assert hasher.verify("not-a-hash", "x") is False


# --------------------------------------------------------------------------- JWT
def test_jwt_round_trip() -> None:
    service = make_jwt()
    user_id = uuid.uuid4()
    token, expires_at = service.issue(user_id=user_id, role=UserRole.EDITOR, now=NOW)
    claims = service.verify(token, now=NOW)
    assert claims.user_id == user_id
    assert claims.role is UserRole.EDITOR
    assert expires_at - NOW == timedelta(minutes=15)


def test_jwt_expiry_follows_the_injected_clock() -> None:
    service = make_jwt(leeway=timedelta(0))
    token, expires_at = service.issue(user_id=uuid.uuid4(), role=UserRole.EDITOR, now=NOW)
    service.verify(token, now=expires_at - timedelta(seconds=1))
    with pytest.raises(TokenExpired):
        service.verify(token, now=expires_at)


def test_jwt_leeway_tolerates_small_clock_skew() -> None:
    service = make_jwt(leeway=timedelta(seconds=30))
    token, expires_at = service.issue(user_id=uuid.uuid4(), role=UserRole.EDITOR, now=NOW)
    service.verify(token, now=expires_at + timedelta(seconds=10))
    with pytest.raises(AuthenticationError, match="not valid yet"):
        service.verify(token, now=NOW - timedelta(minutes=5))


@pytest.mark.parametrize(
    "verifier",
    [
        make_jwt(audience="someone-else"),
        make_jwt(issuer="evil"),
        make_jwt(signing_key=b"x" * 32),
    ],
    ids=["wrong-audience", "wrong-issuer", "wrong-key"],
)
def test_jwt_rejects_mismatched_claims_or_key(verifier: JWTService) -> None:
    token, _ = make_jwt().issue(user_id=uuid.uuid4(), role=UserRole.ADMIN, now=NOW)
    with pytest.raises(AuthenticationError):
        verifier.verify(token, now=NOW)


def test_jwt_rejects_alg_none() -> None:
    forged = pyjwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "role": "admin",
            "typ": "access",
            "jti": "x",
            "iss": "nuroflow",
            "aud": "nuroflow-api",
            "iat": int(NOW.timestamp()),
            "nbf": int(NOW.timestamp()),
            "exp": int((NOW + timedelta(hours=1)).timestamp()),
        },
        key="",
        algorithm="none",
    )
    with pytest.raises(AuthenticationError):
        make_jwt().verify(forged, now=NOW)


def test_jwt_rejects_wrong_token_type() -> None:
    payload = {
        "sub": str(uuid.uuid4()),
        "role": "admin",
        "typ": "refresh",
        "jti": "x",
        "iss": "nuroflow",
        "aud": "nuroflow-api",
        "iat": int(NOW.timestamp()),
        "nbf": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(hours=1)).timestamp()),
    }
    token = pyjwt.encode(payload, KEY, algorithm="HS256")
    with pytest.raises(AuthenticationError, match="token type"):
        make_jwt().verify(token, now=NOW)


# --------------------------------------------------------------------------- refresh tokens
def test_refresh_tokens_are_random_and_hash_deterministically() -> None:
    a, b = generate_refresh_token(), generate_refresh_token()
    assert a != b
    assert len(a) >= 43
    assert hash_refresh_token(a) == hash_refresh_token(a)
    assert len(hash_refresh_token(a)) == 64


# --------------------------------------------------------------------------- API keys
def test_api_key_generate_parse_verify() -> None:
    codec = ApiKeyCodec(pepper=KEY)
    generated = codec.generate()
    assert generated.plaintext.startswith(f"nf_{generated.prefix}_")
    prefix, secret = codec.parse(generated.plaintext)
    assert prefix == generated.prefix
    assert codec.verify(secret, generated.secret_hash)
    assert generated.secret_hash not in generated.plaintext


def test_api_key_hash_depends_on_server_pepper() -> None:
    generated = ApiKeyCodec(pepper=KEY).generate()
    _, secret = ApiKeyCodec(pepper=KEY).parse(generated.plaintext)
    assert not ApiKeyCodec(pepper=b"other" * 8).verify(secret, generated.secret_hash)


@pytest.mark.parametrize(
    "value", ["", "nf_short_x", "sk-proj-123", "nf_ABCDEFGHIJKL_" + "a" * 43, "nf_abc def_x"]
)
def test_api_key_parse_rejects_malformed(value: str) -> None:
    with pytest.raises(AuthenticationError, match="Malformed"):
        ApiKeyCodec(pepper=KEY).parse(value)
