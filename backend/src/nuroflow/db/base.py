"""Declarative base, naming conventions and shared column types.

Three decisions live here so no model has to repeat them:

1. **Naming convention.** Every index and constraint gets a deterministic name. Without
   one, Postgres generates names and Alembic can't reliably drop or alter constraints
   in later migrations.
2. **Type map.** ``Mapped[datetime]`` always means ``TIMESTAMP WITH TIME ZONE`` (naive
   datetimes are a classic scheduler bug), ``Mapped[dict[str, Any]]`` means ``JSONB``, and
   ``Mapped[uuid.UUID]`` means native ``uuid``.
3. **String enums.** :func:`str_enum` stores Python ``StrEnum`` values as ``VARCHAR`` plus
   a named ``CHECK`` constraint (ADR-0006).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Root of every ORM model."""

    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

    # Fetch server-generated values (created_at, updated_at via onupdate) with RETURNING
    # on INSERT and UPDATE. Without this, reading `updated_at` after a commit triggers a
    # lazy load, which raises MissingGreenlet under asyncio.
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012

    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this class attribute
        datetime: sa.TIMESTAMP(timezone=True),
        uuid.UUID: postgresql.UUID(as_uuid=True),
        dict[str, Any]: postgresql.JSONB(none_as_null=True),
        list[str]: postgresql.ARRAY(sa.Text()),
    }


def str_enum[E: StrEnum](enum_cls: type[E], *, name: str, length: int = 32) -> sa.Enum:
    """Map a ``StrEnum`` to ``VARCHAR(length)`` with a named CHECK constraint.

    ``name`` becomes the constraint suffix: ``ck_<table>_<name>``.
    """
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=length,
        validate_strings=True,
        values_callable=lambda members: [m.value for m in members],
    )


EMPTY_JSON_OBJECT = sa.text("'{}'::jsonb")
EMPTY_TEXT_ARRAY = sa.text("'{}'::text[]")
