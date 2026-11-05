"""Static analysis of Jinja templates inside workflow inputs.

We parse every templated string at **save time** to:

1. reject syntax errors before a run ever starts, and
2. find every ``tasks.<key>`` reference, so the spec validator can prove each one
   points at an *upstream ancestor*. Reading the output of a task that may not have
   run yet is a race condition, and we turn it into a validation error.

Parsing only builds an AST; nothing is executed here.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from jinja2 import Environment, TemplateSyntaxError, nodes

_PARSER = Environment(autoescape=False)  # noqa: S701 - parse-only; output is never HTML


@dataclass(frozen=True, slots=True)
class TemplateIssue:
    path: str
    message: str


def is_template(value: str) -> bool:
    return "{{" in value or "{%" in value


def iter_strings(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Yield ``(json_path, string)`` for every string nested in dicts/lists."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from iter_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from iter_strings(v, f"{path}[{i}]")


def analyse(value: Any, path: str = "inputs") -> tuple[set[str], list[TemplateIssue]]:
    """Return (referenced task keys, issues) for every template inside ``value``."""
    refs: set[str] = set()
    issues: list[TemplateIssue] = []
    for sub_path, text in iter_strings(value, path):
        if not is_template(text):
            continue
        try:
            ast = _PARSER.parse(text)
        except TemplateSyntaxError as exc:
            issues.append(TemplateIssue(sub_path, f"template syntax error: {exc.message}"))
            continue
        found, dynamic = _task_references(ast)
        refs |= found
        if dynamic:
            issues.append(
                TemplateIssue(
                    sub_path,
                    "dynamic task references (tasks[variable]) are not allowed; "
                    "use tasks.<key> or tasks['<key>']",
                )
            )
    return refs, issues


def _task_references(ast: nodes.Template) -> tuple[set[str], bool]:
    refs: set[str] = set()
    dynamic = False
    for node in ast.find_all((nodes.Getattr, nodes.Getitem)):
        if isinstance(node, nodes.Getattr):
            if _is_tasks(node.node):
                refs.add(node.attr)
        elif isinstance(node, nodes.Getitem) and _is_tasks(node.node):
            if isinstance(node.arg, nodes.Const) and isinstance(node.arg.value, str):
                refs.add(node.arg.value)
            else:
                dynamic = True
    return refs, dynamic


def _is_tasks(node: nodes.Node) -> bool:
    return isinstance(node, nodes.Name) and node.name == "tasks"
