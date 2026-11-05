"""Sandboxed Jinja rendering of task inputs.

* ``SandboxedEnvironment`` blocks access to Python internals (``__class__``,
  ``__subclasses__``, …), so a workflow author can't escape into the worker process.
* ``StrictUndefined``: a typo like ``{{ tasks.qualify.output.scroe }}`` fails the task
  with a clear error instead of silently rendering an empty string into a prompt.
* **Native values**: a string that is *exactly one* expression returns the
  underlying value, so ``"{{ tasks.qualify.output.score }}"`` yields the int ``87``,
  not the string ``"87"``. Mixed text always renders to a string.
* ``tojson`` is overridden to plain JSON. Jinja's default HTML-escapes ``<``, ``>``
  and ``&``, which is right for web pages and wrong for LLM prompts.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from jinja2 import StrictUndefined, TemplateError, Undefined
from jinja2.sandbox import SandboxedEnvironment

from nuroflow.domain.templates import is_template
from nuroflow.operators.base import NonRetryableError

_SINGLE_EXPRESSION = re.compile(r"^\s*\{\{(?P<expr>(?:(?!\}\}|\{\{|\{%).)*)\}\}\s*$", re.DOTALL)


class TemplateRenderError(NonRetryableError):
    pass


def _tojson(value: Any, indent: int | None = None) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, indent=indent)


class TemplateRenderer:
    def __init__(self) -> None:
        self._env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
        self._env.filters["tojson"] = _tojson
        self._compile_template = lru_cache(maxsize=2048)(self._env.from_string)
        self._compile_expression = lru_cache(maxsize=2048)(
            lambda src: self._env.compile_expression(src, undefined_to_none=False)
        )

    def render(self, value: Any, context: dict[str, Any], *, path: str = "inputs") -> Any:
        if isinstance(value, dict):
            return {k: self.render(v, context, path=f"{path}.{k}") for k, v in value.items()}
        if isinstance(value, list):
            return [self.render(v, context, path=f"{path}[{i}]") for i, v in enumerate(value)]
        if not isinstance(value, str) or not is_template(value):
            return value
        try:
            if match := _SINGLE_EXPRESSION.match(value):
                result = self._compile_expression(match["expr"].strip())(**context)
                if isinstance(result, Undefined):
                    result._fail_with_undefined_error()
                return result
            return self._compile_template(value).render(context)
        except TemplateError as exc:
            raise TemplateRenderError(f"{path}: {exc.message or type(exc).__name__}") from exc
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            raise TemplateRenderError(f"{path}: {type(exc).__name__}: {exc}") from exc
