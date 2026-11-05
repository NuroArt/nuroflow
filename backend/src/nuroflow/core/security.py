"""Security primitives: password hashing, JWTs, refresh tokens, API keys.

Every class here is small, stateless (apart from its keys) and I/O-free, so it can be
unit-tested exhaustively and injected anywhere.

Credential type → storage choice (and why):

======================  ==========================  ================================
Credential              Stored as                   Why
======================  ==========================  ================================
Password                Argon2id PHC string         Low-entropy human secret → needs
                                                    a slow, memory-hard hash
Refresh token           SHA-256 hex                 256 random bits → brute force is
                                                    impossible; fast lookup by hash
API key secret          HMAC-SHA256 (derived key)   256 random bits + server-side
                                                    pepper: a DB dump alone is useless
Access token (JWT)      Not stored                  Stateless, 15 min TTL
======================  ==========================  ================================

A slow hash for API keys would add ~50 ms of CPU to *every* machine request and buy
nothing, because the secret is already unguessable. This is a deliberate change from
the first architecture draft. See docs/AUTH.md.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher as _Argon2Hasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from nuroflow.core.exceptions import AuthenticationError, TokenExpired
from nuroflow.domain.enums import UserRole


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
class PasswordHasher:
    """Argon2id with library defaults (OWASP-compliant parameters)."""

    def __init__(self) -> None:
        self._hasher = _Argon2Hasher()
        # Verified against when the user doesn't exist, so "unknown email" and
        # "wrong password" take the same time (prevents user enumeration by timing).
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(16))

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str | None, password: str) -> bool:
        target = password_hash or self._dummy_hash
        try:
            ok = self._hasher.verify(target, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        return ok and password_hash is not None

    def needs_rehash(self, password_hash: str) -> bool:
        """True when parameters have been strengthened since this hash was created."""
        return self._hasher.check_needs_rehash(password_hash)


# --------------------------------------------------------------------------- #
# Access tokens (JWT)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    role: UserRole
    token_id: str
    issued_at: datetime
    expires_at: datetime


class JWTService:
    """Issues and verifies short-lived access tokens."""

    TOKEN_TYPE = "access"  # noqa: S105 - JWT "typ" claim, not a secret

    def __init__(
        self,
        *,
        signing_key: bytes,
        algorithm: str,
        issuer: str,
        audience: str,
        ttl: timedelta,
        leeway: timedelta = timedelta(seconds=30),
    ) -> None:
        self._key = signing_key
        self._algorithm = algorithm
        self._issuer = issuer
        self._audience = audience
        self.ttl = ttl
        self._leeway = leeway

    def issue(self, *, user_id: uuid.UUID, role: UserRole, now: datetime) -> tuple[str, datetime]:
        expires_at = now + self.ttl
        payload: dict[str, Any] = {
            "sub": str(user_id),
            "role": role.value,
            "typ": self.TOKEN_TYPE,
            "jti": secrets.token_urlsafe(16),
            "iss": self._issuer,
            "aud": self._audience,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        return jwt.encode(payload, self._key, algorithm=self._algorithm), expires_at

    def verify(self, token: str, *, now: datetime) -> AccessTokenClaims:
        """Verify signature, issuer, audience and type, then check time claims against ``now``.

        PyJWT would compare ``exp``/``nbf`` with the wall clock. We disable that and
        check against the injected clock instead, so token expiry follows the same time
        source as everything else and tests can control it.
        """
        try:
            payload = jwt.decode(
                token,
                self._key,
                algorithms=[self._algorithm],  # pinned: blocks alg=none / alg-confusion
                issuer=self._issuer,
                audience=self._audience,
                options={
                    "require": ["sub", "exp", "iat", "nbf", "jti", "typ", "iss", "aud"],
                    "verify_exp": False,
                    "verify_nbf": False,
                    "verify_iat": False,
                },
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError("Invalid access token") from exc

        if payload.get("typ") != self.TOKEN_TYPE:
            raise AuthenticationError("Invalid token type")
        try:
            user_id = uuid.UUID(payload["sub"])
            role = UserRole(payload["role"])
            issued_at = datetime.fromtimestamp(int(payload["iat"]), UTC)
            not_before = datetime.fromtimestamp(int(payload["nbf"]), UTC)
            expires_at = datetime.fromtimestamp(int(payload["exp"]), UTC)
        except (ValueError, KeyError, TypeError) as exc:
            raise AuthenticationError("Invalid access token claims") from exc

        if now >= expires_at + self._leeway:
            raise TokenExpired("Access token has expired")
        if now < not_before - self._leeway:
            raise AuthenticationError("Access token is not valid yet")

        return AccessTokenClaims(
            user_id=user_id,
            role=role,
            token_id=payload["jti"],
            issued_at=issued_at,
            expires_at=expires_at,
        )


# --------------------------------------------------------------------------- #
# Refresh tokens
# --------------------------------------------------------------------------- #
def generate_refresh_token() -> str:
    """256-bit opaque token, URL-safe. Shown to the client once and stored only as a hash."""
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #
_API_KEY_RE = re.compile(r"^nf_(?P<prefix>[a-z0-9]{12})_(?P<secret>[A-Za-z0-9_-]{43})$")


@dataclass(frozen=True, slots=True)
class GeneratedApiKey:
    plaintext: str  # shown once, never stored
    prefix: str  # stored, used for lookup and display ("nf_ab12cd34ef56_…")
    secret_hash: str  # stored


class ApiKeyCodec:
    """Format: ``nf_<12-char prefix>_<43-char secret>``.

    * The recognisable ``nf_`` prefix lets secret scanners (GitHub, GitGuardian)
      detect leaked keys in public repos.
    * The prefix is a lookup handle: O(1) indexed fetch, then a constant-time compare.
    """

    def __init__(self, *, pepper: bytes) -> None:
        self._pepper = pepper

    def generate(self) -> GeneratedApiKey:
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        prefix = "".join(secrets.choice(alphabet) for _ in range(12))
        secret = secrets.token_urlsafe(32)  # 43 chars
        return GeneratedApiKey(
            plaintext=f"nf_{prefix}_{secret}", prefix=prefix, secret_hash=self._hash(secret)
        )

    def parse(self, plaintext: str) -> tuple[str, str]:
        """Split a presented key into (prefix, secret), or raise AuthenticationError."""
        match = _API_KEY_RE.fullmatch(plaintext.strip())
        if match is None:
            raise AuthenticationError("Malformed API key")
        return match["prefix"], match["secret"]

    def verify(self, secret: str, secret_hash: str) -> bool:
        return hmac.compare_digest(self._hash(secret), secret_hash)

    def _hash(self, secret: str) -> str:
        return hmac.new(self._pepper, secret.encode(), hashlib.sha256).hexdigest()
