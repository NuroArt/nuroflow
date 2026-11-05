"""Encrypted credentials for external systems (Telegram, OpenAI, Anthropic, HTTP)."""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from nuroflow.db.base import EMPTY_JSON_OBJECT, Base, str_enum
from nuroflow.db.mixins import TimestampMixin, UUIDPrimaryKeyMixin
from nuroflow.domain.enums import ConnectionType


class Connection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A named credential that workflow tasks reference, e.g. ``connection: sales-bot``.

    * ``encrypted_secret`` holds Fernet ciphertext (key from ``NUROFLOW_ENCRYPTION_KEYS``,
      Phase 2). The API never returns it.
    * ``config`` holds non-secret settings (``base_url``, ``default_model``, ``chat_id``).
    """

    __tablename__ = "connections"
    __table_args__ = (sa.UniqueConstraint("owner_id", "name"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(sa.String(100))
    type: Mapped[ConnectionType] = mapped_column(str_enum(ConnectionType, name="type"))
    description: Mapped[str | None] = mapped_column(sa.Text)
    encrypted_secret: Mapped[bytes] = mapped_column(sa.LargeBinary)
    config: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_JSON_OBJECT)
