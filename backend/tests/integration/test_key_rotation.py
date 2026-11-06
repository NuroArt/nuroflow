"""Encryption key rotation: prepend a key, rotate, drop the old key, and nothing is lost."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session, sessionmaker

from nuroflow.core.crypto import DecryptionError, SecretBox
from nuroflow.core.ids import uuid7
from nuroflow.db.models import Connection, WorkflowTrigger
from nuroflow.domain.enums import ConnectionType, TriggerType
from nuroflow.maintenance import rotate_encryption
from tests.conftest import EngineHarness

pytestmark = pytest.mark.integration


def test_rotation_moves_every_secret_to_the_new_key(
    harness: EngineHarness, sync_sessions: sessionmaker[Session]
) -> None:
    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    old_box = SecretBox([old_key])
    workflow_id, _ = harness.create_workflow(
        {"slug": "hooks", "name": "hooks", "tasks": [{"key": "a", "operator": "noop"}]}
    )
    with sync_sessions() as s, s.begin():
        for i in range(5):  # more rows than one batch, to exercise keyset paging
            s.add(
                Connection(
                    id=uuid7(),
                    owner_id=harness.owner_id,
                    name=f"key-{i}",
                    type=ConnectionType.OPENAI,
                    encrypted_secret=old_box.encrypt(f"sk-secret-{i}"),
                    config={},
                )
            )
        s.add(
            WorkflowTrigger(
                id=uuid7(),
                workflow_id=workflow_id,
                type=TriggerType.WEBHOOK,
                spec_key="webhook:default",
                name="default",
                webhook_secret_encrypted=old_box.encrypt("whsec_hook"),
            )
        )

    # Step 1+2: new key first, old key still accepted; rotate.
    counts = rotate_encryption(sync_sessions, SecretBox([new_key, old_key]), batch_size=2)
    assert counts == {"connections": 5, "workflow_triggers": 1}

    # Step 3: the old key is gone, yet every secret still decrypts.
    new_only = SecretBox([new_key])
    with sync_sessions() as s:
        secrets = sorted(
            new_only.decrypt(c) for c in s.scalars(sa.select(Connection.encrypted_secret))
        )
        hook = s.scalar(sa.select(WorkflowTrigger.webhook_secret_encrypted))
    assert secrets == [f"sk-secret-{i}" for i in range(5)]
    assert hook is not None
    assert new_only.decrypt(hook) == "whsec_hook"
    with pytest.raises(DecryptionError):
        old_box.decrypt(hook)

    # Idempotent: running again is harmless.
    assert rotate_encryption(sync_sessions, SecretBox([new_key]))["connections"] == 5
