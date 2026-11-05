"""Identity and access: users, refresh tokens, API keys."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nuroflow.db.base import EMPTY_TEXT_ARRAY, Base, str_enum
from nuroflow.db.mixins import CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin
from nuroflow.domain.enums import UserRole


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        # Case-insensitive uniqueness without the citext extension.
        sa.Index("uq_users_email_lower", sa.text("lower(email)"), unique=True),
    )

    email: Mapped[str] = mapped_column(sa.String(320))  # RFC 5321 max length
    hashed_password: Mapped[str] = mapped_column(sa.String(255))  # Argon2id PHC string
    full_name: Mapped[str | None] = mapped_column(sa.String(200))
    role: Mapped[UserRole] = mapped_column(
        str_enum(UserRole, name="role"), server_default=UserRole.EDITOR.value
    )
    is_active: Mapped[bool] = mapped_column(server_default=sa.true())
    last_login_at: Mapped[datetime | None]

    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )
    api_keys: Mapped[list[ApiKey]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True, lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r} role={self.role}>"


class RefreshToken(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Opaque, rotating refresh token. Only the SHA-256 hash is stored.

    ``family_id`` groups every token in one rotation chain. If an already-rotated token
    is presented again, the whole family is revoked, because that's how refresh-token
    theft shows up.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(sa.CHAR(64), unique=True)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("refresh_tokens.id", ondelete="SET NULL")
    )
    user_agent: Mapped[str | None] = mapped_column(sa.String(512))
    ip_address: Mapped[str | None] = mapped_column(postgresql.INET)

    user: Mapped[User] = relationship(back_populates="refresh_tokens", lazy="raise")


class ApiKey(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Machine credential for event ingestion and automation.

    Format shown once to the user: ``nf_<prefix>_<secret>``. ``prefix`` is stored in
    plaintext for O(1) lookup; the secret is verified against ``key_hash``, a peppered
    HMAC-SHA256 (see docs/AUTH.md for why not Argon2).
    """

    __tablename__ = "api_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(sa.String(100))
    prefix: Mapped[str] = mapped_column(sa.String(16), unique=True)
    key_hash: Mapped[str] = mapped_column(sa.String(255))
    scopes: Mapped[list[str]] = mapped_column(server_default=EMPTY_TEXT_ARRAY)
    last_used_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]

    user: Mapped[User] = relationship(back_populates="api_keys", lazy="raise")
