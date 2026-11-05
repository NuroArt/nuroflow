"""Pure engine rules: DAG, trigger rules, state machine, spec validation, templating, retries."""

from __future__ import annotations

import random
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from nuroflow.domain.dag import DAG, CycleError, UnknownDependencyError
from nuroflow.domain.enums import RunStatus, TaskStatus
from nuroflow.domain.state import (
    InvalidStateTransition,
    Readiness,
    TriggerRule,
    ensure_run_transition,
    ensure_task_transition,
    evaluate_trigger_rule,
    final_run_status,
)
from nuroflow.domain.workflow_spec import WorkflowSpec
from nuroflow.engine.retry import RetryPolicy
from nuroflow.engine.templating import TemplateRenderer, TemplateRenderError

EXAMPLE = Path(__file__).resolve().parents[3] / "examples/workflows/lead-qualification.yaml"
T = TaskStatus


def example() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text())
    return data


def minimal(**overrides: Any) -> dict[str, Any]:
    spec = {"slug": "demo", "name": "Demo", "tasks": [{"key": "a", "operator": "noop"}]}
    spec.update(overrides)
    return spec


# --------------------------------------------------------------------------- DAG
def test_dag_order_roots_and_ancestors() -> None:
    dag = DAG.build([("a", []), ("b", ["a"]), ("c", ["a"]), ("d", ["b", "c"])])
    assert dag.roots == ("a",)
    assert dag.topological_order == ("a", "b", "c", "d")
    assert dag.ancestors("d") == {"a", "b", "c"}
    assert dag.downstream["a"] == ("b", "c")


def test_dag_reports_the_cycle_path() -> None:
    with pytest.raises(CycleError) as exc_info:
        DAG.build([("a", ["c"]), ("b", ["a"]), ("c", ["b"]), ("x", [])])
    cycle = exc_info.value.cycle
    assert cycle[0] == cycle[-1]
    assert set(cycle) == {"a", "b", "c"}


@pytest.mark.parametrize(
    ("edges", "error"),
    [
        ([("a", ["a"])], CycleError),
        ([("a", ["ghost"])], UnknownDependencyError),
        ([("a", []), ("a", [])], ValueError),
    ],
    ids=["self-loop", "unknown-dep", "duplicate-key"],
)
def test_dag_rejects_invalid_graphs(edges: list[tuple[str, list[str]]], error: type) -> None:
    with pytest.raises(error):
        DAG.build(edges)


# --------------------------------------------------------------------------- trigger rules
@pytest.mark.parametrize(
    ("rule", "upstream", "expected"),
    [
        (TriggerRule.ALL_SUCCESS, [], Readiness.READY),
        (TriggerRule.ALL_SUCCESS, [T.SUCCEEDED, T.RUNNING], Readiness.WAIT),
        (TriggerRule.ALL_SUCCESS, [T.SUCCEEDED, T.SUCCEEDED], Readiness.READY),
        (TriggerRule.ALL_SUCCESS, [T.SUCCEEDED, T.SKIPPED], Readiness.SKIP),
        (TriggerRule.ALL_SUCCESS, [T.FAILED], Readiness.SKIP),
        (TriggerRule.NONE_FAILED, [T.SUCCEEDED, T.SKIPPED], Readiness.READY),
        (TriggerRule.NONE_FAILED, [T.SUCCEEDED, T.FAILED], Readiness.SKIP),
        (TriggerRule.ONE_FAILED, [T.SUCCEEDED, T.FAILED], Readiness.READY),
        (TriggerRule.ONE_FAILED, [T.SUCCEEDED, T.SUCCEEDED], Readiness.SKIP),
        (TriggerRule.ALL_DONE, [T.FAILED, T.SKIPPED], Readiness.READY),
        (TriggerRule.ALL_DONE, [T.FAILED, T.UP_FOR_RETRY], Readiness.WAIT),
    ],
)
def test_trigger_rules(rule: TriggerRule, upstream: list[TaskStatus], expected: Readiness) -> None:
    assert evaluate_trigger_rule(rule, upstream) is expected


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([T.SUCCEEDED, T.SKIPPED], RunStatus.SUCCEEDED),
        ([T.SUCCEEDED, T.FAILED], RunStatus.FAILED),
        ([T.FAILED, T.SUCCEEDED], RunStatus.FAILED),  # handled error is still an error
        ([T.CANCELLED, T.CANCELLED], RunStatus.CANCELLED),
        ([T.SUCCEEDED, T.RUNNING], None),
    ],
)
def test_final_run_status(statuses: list[TaskStatus], expected: RunStatus | None) -> None:
    assert final_run_status(statuses) is expected


def test_illegal_transitions_raise() -> None:
    ensure_task_transition(T.RUNNING, T.UP_FOR_RETRY)
    with pytest.raises(InvalidStateTransition, match="succeeded → running"):
        ensure_task_transition(T.SUCCEEDED, T.RUNNING)
    with pytest.raises(InvalidStateTransition):
        ensure_run_transition(RunStatus.FAILED, RunStatus.RUNNING)


# --------------------------------------------------------------------------- spec
def test_example_workflow_is_valid_and_resolves_defaults() -> None:
    spec = WorkflowSpec.model_validate(example())
    assert spec.dag.topological_order == ("qualify", "store", "summarize", "alert")
    qualify = spec.task("qualify")
    assert qualify.retries == 3  # task override
    assert qualify.max_attempts == 4
    assert spec.task("store").retries == 2  # inherited from defaults
    assert spec.task("store").timeout_seconds == 120


def test_checksum_is_stable_and_content_sensitive() -> None:
    a = WorkflowSpec.model_validate(example())
    b = WorkflowSpec.model_validate(example())
    changed = example()
    changed["tasks"][0]["retries"] = 1
    assert a.checksum() == b.checksum()
    assert a.checksum() != WorkflowSpec.model_validate(changed).checksum()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["tasks"][0].update(depend_on=["x"]), "Extra inputs are not permitted"),
        (lambda d: d["tasks"][0].update(depends_on=["alert"]), "dependency cycle"),
        (
            lambda d: d["tasks"][1].update(depends_on=[]),
            "references tasks.qualify, which is not upstream",
        ),
        (
            lambda d: d["tasks"][3]["inputs"].update(text="{{ tasks.nope.output }}"),
            "it is not defined",
        ),
        (
            lambda d: d["tasks"][3]["inputs"].update(text="{{ tasks[name] }}"),
            "dynamic task references",
        ),
        (lambda d: d["tasks"][3]["inputs"].update(text="{{ unclosed "), "template syntax error"),
        (lambda d: d["tasks"][0].update(key="Bad-Key"), "String should match pattern"),
        (
            lambda d: d.update(triggers=[{"type": "cron", "cron": "61 * * * *"}]),
            "invalid 5-field cron",
        ),
        (
            lambda d: d.update(
                triggers=[{"type": "cron", "cron": "0 9 * * 1", "timezone": "Mars/Base"}]
            ),
            "unknown IANA timezone",
        ),
        (lambda d: d.update(tasks=[]), "at least 1 item"),
    ],
    ids=[
        "typo",
        "cycle",
        "not-upstream",
        "undefined-task",
        "dynamic",
        "syntax",
        "key",
        "cron",
        "tz",
        "empty",
    ],
)
def test_spec_rejects_authoring_mistakes(mutate: Any, message: str) -> None:
    data = example()
    mutate(data)
    with pytest.raises(ValueError, match=message):
        WorkflowSpec.model_validate(data)


def test_yaml_on_key_gotcha_is_avoided() -> None:
    """`events:` (not `on:`) survives YAML 1.1, where a bare `on` key becomes True."""
    spec = WorkflowSpec.model_validate(example())
    assert [e.value for e in spec.notifications[0].events] == ["run.failed"]


# --------------------------------------------------------------------------- templating
@pytest.fixture(scope="module")
def renderer() -> TemplateRenderer:
    return TemplateRenderer()


CTX: dict[str, Any] = {
    "params": {"chat": "42"},
    "trigger": {"type": "event", "payload": {"name": "Ada <CTO>", "email": "ada@x.io"}},
    "tasks": {"qualify": {"output": {"score": 87, "tier": "hot"}, "status": "succeeded"}},
}


def test_single_expression_keeps_native_type(renderer: TemplateRenderer) -> None:
    assert renderer.render("{{ tasks.qualify.output.score }}", CTX) == 87
    assert renderer.render(" {{ tasks.qualify.output }} ", CTX) == {"score": 87, "tier": "hot"}


def test_mixed_text_renders_string_and_recurses(renderer: TemplateRenderer) -> None:
    value = {"a": ["Lead {{ trigger.payload.name }} is {{ tasks.qualify.output.tier | upper }}"]}
    assert renderer.render(value, CTX) == {"a": ["Lead Ada <CTO> is HOT"]}


def test_tojson_is_not_html_escaped(renderer: TemplateRenderer) -> None:
    assert (
        renderer.render("{{ trigger.payload | tojson }}", CTX)
        == '{"name": "Ada <CTO>", "email": "ada@x.io"}'
    )


def test_undefined_values_fail_loudly(renderer: TemplateRenderer) -> None:
    with pytest.raises(TemplateRenderError, match=r"inputs\.x"):
        renderer.render({"x": "{{ tasks.qualify.output.scroe }}"}, CTX)
    with pytest.raises(TemplateRenderError):
        renderer.render("{{ missing }}", CTX)


@pytest.mark.parametrize(
    "attack",
    [
        "{{ ''.__class__.__mro__[1].__subclasses__() }}",
        "{{ params.__class__.__init__.__globals__ }}",
        "{{ cycler.__init__.__globals__.os.popen('id').read() }}",
    ],
)
def test_sandbox_blocks_python_internals(renderer: TemplateRenderer, attack: str) -> None:
    with pytest.raises(TemplateRenderError):
        renderer.render(attack, deepcopy(CTX))


# --------------------------------------------------------------------------- retry policy
def test_retry_delay_grows_exponentially_with_bounded_jitter() -> None:
    policy = RetryPolicy(base_seconds=10, max_seconds=60, rng=random.Random(7))
    for attempt, raw in [(1, 10), (2, 20), (3, 40), (4, 60), (9, 60)]:
        delay = policy.delay(attempt)
        assert timedelta(seconds=raw / 2) <= delay <= timedelta(seconds=raw)


def test_retry_attempt_numbers_are_one_based() -> None:
    with pytest.raises(ValueError, match="1-based"):
        RetryPolicy(base_seconds=1, max_seconds=1).delay(0)
