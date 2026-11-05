"""Shared schema building blocks: base config and cursor pagination."""

from __future__ import annotations

import base64
import binascii
import uuid

from pydantic import BaseModel, ConfigDict

from nuroflow.core.exceptions import ValidationError


class Schema(BaseModel):
    """Base for all API schemas: strict about unknown fields, ORM-friendly."""

    model_config = ConfigDict(from_attributes=True, extra="forbid", str_strip_whitespace=True)


class Page[T](BaseModel):
    """Cursor page. Pass ``next_cursor`` back as ``?cursor=`` for the next page."""

    items: list[T]
    next_cursor: str | None = None


def encode_cursor(last_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(last_id.bytes).rstrip(b"=").decode()


def decode_cursor(cursor: str | None) -> uuid.UUID | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return uuid.UUID(bytes=base64.urlsafe_b64decode(padded))
    except (ValueError, binascii.Error) as exc:
        raise ValidationError("Invalid pagination cursor", field="cursor") from exc
