"""WorkflowSpec: the validated, versioned definition of a workflow (ADR-0004).

A workflow is **data**: YAML/JSON that this model validates completely before it's
saved. Everything that can be checked without running anything is checked here:

* field formats, limits, and unknown fields (typos like ``depend_on`` are errors);
* unique task keys, known dependencies, and **no cycles** (with the cycle path in the error);
* template syntax, and that every ``tasks.<key>`` reference points at an upstream ancestor;
* cron expressions and timezones.

Checks that need the operator registry (does ``ai.structured`` exist, are its inputs
well-formed?) live in ``services.workflows`` because they depend on installed plugins.
"""

from __future__ import annotations

import hashlib
import json
import zoneinfo
from functools import cached_property
from typing import Annotated, Any, Literal, Self

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nuroflow.domain.dag import DAG, CycleError, UnknownDependencyError
from nuroflow.domain.enums import RunEvent
from nuroflow.domain.state import TriggerRule
from nuroflow.domain.templates import analyse

TASK_KEY = r"^[a-z][a-z0-9_]{0,62}$"  # identifier-safe: usable as tasks.<key> in templates
SLUG = r"^[a-z0-9][a-z0-9-]{0,99}$"
OPERATOR_NAME = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$"
QUEUE_NAME = r"^[a-z][a-z0-9_-]{0,49}$"
MAX_TASKS = 200


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskDefaults(_Model):
    retries: int = Field(default=0, ge=0, le=10)
    retry_backoff_seconds: float = Field(default=10, ge=0, le=3600)
    retry_backoff_max_seconds: float = Field(default=600, ge=0, le=86_400)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)


class TaskSpec(_Model):
    key: str = Field(pattern=TASK_KEY)
    operator: str = Field(pattern=OPERATOR_NAME, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    depends_on: list[str] = Field(default_factory=list, max_length=50)
    inputs: dict[str, Any] = Field(default_factory=dict)
    trigger_rule: TriggerRule = TriggerRule.ALL_SUCCESS
    queue: str | None = Field(default=None, pattern=QUEUE_NAME)
    # None = inherit from WorkflowSpec.defaults
    retries: int | None = Field(default=None, ge=0, le=10)
    retry_backoff_seconds: float | None = Field(default=None, ge=0, le=3600)
    retry_backoff_max_seconds: float | None = Field(default=None, ge=0, le=86_400)
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)


class ResolvedTask(_Model):
    """A task with defaults applied: exactly what the engine executes."""

    key: str
    operator: str
    inputs: dict[str, Any]
    depends_on: tuple[str, ...]
    trigger_rule: TriggerRule
    queue: str | None
    retries: int
    retry_backoff_seconds: float
    retry_backoff_max_seconds: float
    timeout_seconds: int

    @property
    def max_attempts(self) -> int:
        return self.retries + 1


class CronTriggerSpec(_Model):
    """Identity (``spec_key``) is the schedule itself. Changing ``catchup`` keeps the
    trigger's position in time; changing the expression starts a new trigger."""

    type: Literal["cron"]
    cron: str = Field(max_length=100, examples=["*/15 * * * *"])
    timezone: str = "UTC"
    catchup: bool = False

    @field_validator("cron")
    @classmethod
    def _valid_cron(cls, value: str) -> str:
        if len(value.split()) != 5 or not croniter.is_valid(value):
            raise ValueError(f"invalid 5-field cron expression: {value!r}")
        return value

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            zoneinfo.ZoneInfo(value)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {value!r}") from exc
        return value

    @property
    def spec_key(self) -> str:
        return f"cron:{self.cron}:{self.timezone}"


class EventTriggerSpec(_Model):
    type: Literal["event"]
    event_type: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,199}$", examples=["lead.captured"])
    event_filter: dict[str, Any] | None = None

    @property
    def spec_key(self) -> str:
        canonical = json.dumps(self.event_filter, sort_keys=True, separators=(",", ":"))
        return f"event:{self.event_type}:{hashlib.sha256(canonical.encode()).hexdigest()[:16]}"


class WebhookTriggerSpec(_Model):
    """Identity is the name, so a webhook keeps its URL and secret across versions."""

    type: Literal["webhook"]
    name: str = Field(default="default", pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")

    @property
    def spec_key(self) -> str:
        return f"webhook:{self.name}"


TriggerSpec = Annotated[
    CronTriggerSpec | EventTriggerSpec | WebhookTriggerSpec, Field(discriminator="type")
]


class NotificationSpec(_Model):
    channel: str = Field(min_length=1, max_length=100)
    # Not named `on`: YAML 1.1 parses the bare key `on` as boolean True (the "Norway problem").
    events: list[RunEvent] = Field(min_length=1)


class WorkflowSpec(_Model):
    spec_version: Literal[1] = 1
    slug: str = Field(pattern=SLUG)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    max_active_runs: int = Field(default=16, ge=1, le=1000)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    params: dict[str, Any] = Field(default_factory=dict, description="Default run parameters")
    defaults: TaskDefaults = Field(default_factory=TaskDefaults)
    triggers: list[TriggerSpec] = Field(default_factory=list, max_length=20)
    tasks: list[TaskSpec] = Field(min_length=1, max_length=MAX_TASKS)
    notifications: list[NotificationSpec] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _validate_graph_and_templates(self) -> Self:
        try:
            dag = DAG.build((t.key, t.depends_on) for t in self.tasks)
        except (CycleError, UnknownDependencyError, ValueError) as exc:
            raise ValueError(str(exc)) from exc

        problems: list[str] = []
        keys = [t.spec_key for t in self.triggers]
        if duplicates := sorted({k for k in keys if keys.count(k) > 1}):
            problems.append(f"duplicate triggers: {', '.join(duplicates)}")
        for task in self.tasks:
            refs, issues = analyse(task.inputs, path=f"tasks.{task.key}.inputs")
            problems += [f"{i.path}: {i.message}" for i in issues]
            ancestors = dag.ancestors(task.key)
            for ref in sorted(refs - ancestors):
                hint = (
                    "it is not defined"
                    if ref not in dag.upstream
                    else f"add it to depends_on of '{task.key}' (directly or transitively)"
                )
                problems.append(
                    f"tasks.{task.key}.inputs references tasks.{ref}, which is not upstream: {hint}"
                )
        if problems:
            raise ValueError("; ".join(problems))
        return self

    # ------------------------------------------------------------------ #
    @cached_property
    def dag(self) -> DAG:
        return DAG.build((t.key, t.depends_on) for t in self.tasks)

    def task(self, key: str) -> ResolvedTask:
        for t in self.tasks:
            if t.key == key:
                return self._resolve(t)
        raise KeyError(key)

    def resolved_tasks(self) -> list[ResolvedTask]:
        return [self._resolve(t) for t in self.tasks]

    def _resolve(self, t: TaskSpec) -> ResolvedTask:
        d = self.defaults

        def pick[V](own: V | None, default: V) -> V:
            return default if own is None else own

        return ResolvedTask(
            key=t.key,
            operator=t.operator,
            inputs=t.inputs,
            depends_on=tuple(t.depends_on),
            trigger_rule=t.trigger_rule,
            queue=t.queue,
            retries=pick(t.retries, d.retries),
            retry_backoff_seconds=pick(t.retry_backoff_seconds, d.retry_backoff_seconds),
            retry_backoff_max_seconds=pick(
                t.retry_backoff_max_seconds, d.retry_backoff_max_seconds
            ),
            timeout_seconds=pick(t.timeout_seconds, d.timeout_seconds),
        )

    def canonical_json(self) -> str:
        """Stable serialisation: identical specs → identical bytes → identical checksum."""
        return json.dumps(
            self.model_dump(mode="json", exclude_defaults=False),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()
