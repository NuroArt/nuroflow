"""Operator contract: the only extension point for task behaviour (ADR-0004).

An operator is a small class with:

* ``name``: what workflows reference (``operator: ai.structured``);
* ``input_model``: a Pydantic model, so inputs are validated *after* template rendering;
* ``execute(inputs, ctx)``: does the work and returns a JSON-serialisable dict, which
  becomes ``tasks.<key>.output`` for downstream templates.

Operators signal *intent* through exception types, never through return codes:

* :class:`RetryableError`: transient (timeouts, 429, 5xx, a malformed LLM answer). The
  engine retries with backoff.
* :class:`NonRetryableError`: permanent (bad input, 4xx, unknown provider). It fails
  immediately, because retrying a bug just burns money.
* Any other exception counts as retryable. Unknown errors get the benefit of the
  doubt, and the traceback is recorded on the attempt.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from nuroflow.domain.enums import LogLevel


class TaskError(Exception):
    retryable: ClassVar[bool] = True


class RetryableError(TaskError):
    retryable = True


class NonRetryableError(TaskError):
    retryable = False


class TaskTimeoutError(RetryableError):
    pass


class OperatorInput(BaseModel):
    """Base for operator inputs: unknown keys are errors (catches typos in YAML)."""

    model_config = ConfigDict(extra="forbid")


@dataclass(slots=True)
class LogLine:
    level: LogLevel
    message: str
    context: dict[str, Any]
    at: datetime


@dataclass(slots=True)
class OperatorContext:
    """Everything an operator may know about its execution. No DB session, no secrets store."""

    workflow_id: uuid.UUID
    run_id: uuid.UUID
    task_run_id: uuid.UUID
    task_key: str
    owner_id: uuid.UUID
    attempt: int
    timeout_seconds: int
    logs: list[LogLine] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def idempotency_key(self) -> str:
        """Stable across retries of the *same* task run. Send it to APIs that dedupe."""
        return f"nuroflow:{self.task_run_id}"

    def log(self, message: str, level: LogLevel = LogLevel.INFO, **context: Any) -> None:
        self.logs.append(LogLine(level, message[:10_000], context, datetime.now(UTC)))


class Operator(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    input_model: ClassVar[type[OperatorInput]]
    default_queue: ClassVar[str] = "default"

    def run(self, raw_inputs: dict[str, Any], ctx: OperatorContext) -> dict[str, Any]:
        """Template method: validate rendered inputs, then execute."""
        try:
            inputs = self.input_model.model_validate(raw_inputs)
        except PydanticValidationError as exc:
            raise NonRetryableError(f"invalid inputs for {self.name}: {_summarise(exc)}") from exc
        return self.execute(inputs, ctx)

    @abstractmethod
    def execute(self, inputs: Any, ctx: OperatorContext) -> dict[str, Any]: ...

    @classmethod
    def static_check(cls, inputs: dict[str, Any]) -> list[str]:
        """Save-time checks on *unrendered* inputs (values may still be templates).

        We can't type-check ``"{{ tasks.qualify.output.score }}"`` before it renders, but
        we can catch unknown keys and missing required keys, the two most common
        authoring mistakes.
        """
        # Inputs are addressed by alias when one exists (e.g. http.request's `json`).
        accepted = {f.alias or name: f for name, f in cls.input_model.model_fields.items()}
        problems = [f"unknown input '{k}'" for k in inputs if k not in accepted]
        problems += [
            f"missing required input '{name}'"
            for name, f in accepted.items()
            if f.is_required() and name not in inputs
        ]
        return problems

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "name": cls.name,
            "description": cls.description,
            "default_queue": cls.default_queue,
            "input_schema": cls.input_model.model_json_schema(),
        }


def _summarise(exc: PydanticValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}" for err in exc.errors()
    )
