"""Application error hierarchy.

Services raise these. They know nothing about HTTP. The API layer maps each class to
an RFC 9457 Problem Details response in exactly one place
(``nuroflow.api.errors``). Workers (Phase 3) will map the same errors to task
failures. One hierarchy, many transports.

``code`` is a stable, machine-readable identifier that clients can branch on. Never
change an existing code; add a new one.
"""

from __future__ import annotations

from typing import Any, ClassVar


class NuroFlowError(Exception):
    """Base class for every expected (non-bug) error."""

    status_code: ClassVar[int] = 500
    code: ClassVar[str] = "internal_error"
    title: ClassVar[str] = "Internal error"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        self.detail = detail or self.title
        self.extra = extra
        super().__init__(self.detail)


class ValidationError(NuroFlowError):
    status_code = 422
    code = "validation_error"
    title = "Validation failed"


class AuthenticationError(NuroFlowError):
    """Who are you? (missing, invalid or expired credentials)."""

    status_code = 401
    code = "authentication_failed"
    title = "Authentication failed"


class InvalidCredentials(AuthenticationError):
    code = "invalid_credentials"
    title = "Invalid email or password"


class TokenExpired(AuthenticationError):
    code = "token_expired"
    title = "Token has expired"


class TokenReuseDetected(AuthenticationError):
    """A rotated refresh token was replayed, a strong signal of theft."""

    code = "token_reuse_detected"
    title = "Refresh token reuse detected; all sessions in this family were revoked"


class PermissionDenied(NuroFlowError):
    """I know who you are, but you can't do this."""

    status_code = 403
    code = "permission_denied"
    title = "Permission denied"


class NotFound(NuroFlowError):
    status_code = 404
    code = "not_found"
    title = "Resource not found"


class Conflict(NuroFlowError):
    status_code = 409
    code = "conflict"
    title = "Resource conflict"


class RateLimited(NuroFlowError):
    status_code = 429
    code = "rate_limited"
    title = "Too many requests"

    def __init__(self, detail: str | None = None, *, retry_after: int, **extra: Any) -> None:
        super().__init__(detail, retry_after=retry_after, **extra)
        self.retry_after = retry_after


class FeatureDisabled(NuroFlowError):
    status_code = 403
    code = "feature_disabled"
    title = "This feature is disabled"


class DependencyUnavailable(NuroFlowError):
    status_code = 503
    code = "dependency_unavailable"
    title = "A required dependency is unavailable"
