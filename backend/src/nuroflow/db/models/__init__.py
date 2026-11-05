"""ORM model registry.

Importing this package registers every table on ``Base.metadata``. Alembic's ``env.py``
imports it so autogenerate sees the whole schema.
"""

from nuroflow.db.base import Base
from nuroflow.db.models.connection import Connection
from nuroflow.db.models.event import Event
from nuroflow.db.models.execution import TaskAttempt, TaskLog, TaskRun, WorkflowRun
from nuroflow.db.models.identity import ApiKey, RefreshToken, User
from nuroflow.db.models.notification import (
    NotificationChannel,
    NotificationDelivery,
    NotificationSubscription,
)
from nuroflow.db.models.workflow import Workflow, WorkflowTrigger, WorkflowVersion

__all__ = [
    "ApiKey",
    "Base",
    "Connection",
    "Event",
    "NotificationChannel",
    "NotificationDelivery",
    "NotificationSubscription",
    "RefreshToken",
    "TaskAttempt",
    "TaskLog",
    "TaskRun",
    "User",
    "Workflow",
    "WorkflowRun",
    "WorkflowTrigger",
    "WorkflowVersion",
]
