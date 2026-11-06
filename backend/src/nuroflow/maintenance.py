"""Operational maintenance tasks, run from the ``nuroflow`` CLI.

Key rotation, the safe way:

1. Generate a new Fernet key and **prepend** it: ``NUROFLOW_ENCRYPTION_KEYS=new,old``.
   Deploy. New writes use ``new``; old ciphertext still decrypts with ``old``.
2. Run ``nuroflow rotate-encryption``. Every stored secret is re-encrypted with ``new``.
3. Remove ``old`` from the list and deploy again.

The job is idempotent and resumable. It works in small committed batches, so it never
holds long locks, and rerunning after an interruption simply re-rotates rows that are
already current.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from nuroflow.core.crypto import SecretBox
from nuroflow.db.models import Connection, WorkflowTrigger

logger = logging.getLogger(__name__)

# (model, primary key column, ciphertext column)
ENCRYPTED_COLUMNS: list[tuple[Any, Any, Any]] = [
    (Connection, Connection.id, Connection.encrypted_secret),
    (WorkflowTrigger, WorkflowTrigger.id, WorkflowTrigger.webhook_secret_encrypted),
]


def rotate_encryption(
    session_factory: sessionmaker[Session], secrets: SecretBox, *, batch_size: int = 200
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for model, pk, column in ENCRYPTED_COLUMNS:
        name = model.__tablename__
        counts[name] = 0
        after: uuid.UUID | None = None
        while True:
            with session_factory() as session, session.begin():
                query = sa.select(pk, column).where(column.is_not(None)).order_by(pk)
                if after is not None:
                    query = query.where(pk > after)
                rows = session.execute(
                    query.limit(batch_size).with_for_update(skip_locked=False)
                ).all()
                for row_id, ciphertext in rows:
                    session.execute(
                        sa.update(model)
                        .where(pk == row_id)
                        .values({column.key: secrets.rotate(ciphertext)})
                    )
            if not rows:
                break
            counts[name] += len(rows)
            after = rows[-1][0]
        logger.info("encryption rotated", extra={"table": name, "rows": counts[name]})
    return counts
