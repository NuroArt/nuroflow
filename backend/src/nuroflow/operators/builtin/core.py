"""Control-flow and data-shaping operators: ``noop``, ``control.delay``, ``transform.template``."""

from __future__ import annotations

import time
from typing import Any

from pydantic import Field

from nuroflow.operators.base import Operator, OperatorContext, OperatorInput


class NoopInput(OperatorInput):
    pass


class NoopOperator(Operator):
    """Does nothing. Useful as a join point or a placeholder while designing a DAG."""

    name = "noop"
    description = "Does nothing; succeeds immediately."
    input_model = NoopInput

    def execute(self, inputs: NoopInput, ctx: OperatorContext) -> dict[str, Any]:
        return {}


class DelayInput(OperatorInput):
    seconds: float = Field(ge=0, le=300)


class DelayOperator(Operator):
    """Sleeps inside the worker. For long waits, use a cron trigger instead of holding a slot."""

    name = "control.delay"
    description = "Pause for up to 5 minutes (holds a worker slot)."
    input_model = DelayInput

    def __init__(self, sleep: Any = time.sleep) -> None:
        self._sleep = sleep

    def execute(self, inputs: DelayInput, ctx: OperatorContext) -> dict[str, Any]:
        ctx.log(f"sleeping {inputs.seconds}s")
        self._sleep(inputs.seconds)
        return {"slept_seconds": inputs.seconds}


class TransformInput(OperatorInput):
    value: Any = Field(description="Any JSON value; templates are rendered by the engine first.")


class TransformOperator(Operator):
    """Shape data between tasks, e.g. build a payload from several upstream outputs.

    All the work happens in template rendering; the operator just returns the result.
    """

    name = "transform.template"
    description = "Render a value from templates and pass it downstream as output.value."
    input_model = TransformInput

    def execute(self, inputs: TransformInput, ctx: OperatorContext) -> dict[str, Any]:
        return {"value": inputs.value}
