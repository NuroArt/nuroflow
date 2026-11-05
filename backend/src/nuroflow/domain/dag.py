"""Directed acyclic graph of task dependencies. Pure, immutable, O(V+E).

Built once per validation or engine step from ``(key, depends_on)`` pairs. Gives the
engine everything it needs: roots, topological order, direct upstream/downstream,
and transitive ancestors (used to scope which outputs a template may read).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property


class CycleError(ValueError):
    def __init__(self, cycle: Sequence[str]) -> None:
        self.cycle = list(cycle)
        super().__init__("dependency cycle: " + " → ".join(self.cycle))


class UnknownDependencyError(ValueError):
    def __init__(self, task: str, missing: str) -> None:
        self.task, self.missing = task, missing
        super().__init__(f"task '{task}' depends on unknown task '{missing}'")


@dataclass(frozen=True)
class DAG:
    upstream: Mapping[str, tuple[str, ...]]

    @classmethod
    def build(cls, edges: Iterable[tuple[str, Iterable[str]]]) -> DAG:
        upstream: dict[str, tuple[str, ...]] = {}
        for key, deps in edges:
            if key in upstream:
                raise ValueError(f"duplicate task key '{key}'")
            upstream[key] = tuple(dict.fromkeys(deps))  # de-duplicate, keep order
        for key, deps in upstream.items():
            for dep in deps:
                if dep == key:
                    raise CycleError([key, key])
                if dep not in upstream:
                    raise UnknownDependencyError(key, dep)
        dag = cls(upstream=upstream)
        _ = dag.topological_order  # raises CycleError if not acyclic
        return dag

    # ------------------------------------------------------------------ #
    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self.upstream)

    @cached_property
    def downstream(self) -> Mapping[str, tuple[str, ...]]:
        down: dict[str, list[str]] = {k: [] for k in self.upstream}
        for key, deps in self.upstream.items():
            for dep in deps:
                down[dep].append(key)
        return {k: tuple(v) for k, v in down.items()}

    @cached_property
    def roots(self) -> tuple[str, ...]:
        return tuple(k for k, deps in self.upstream.items() if not deps)

    @cached_property
    def topological_order(self) -> tuple[str, ...]:
        """Kahn's algorithm; ties broken by declaration order for stable, readable output."""
        in_degree = {k: len(deps) for k, deps in self.upstream.items()}
        ready = deque(k for k in self.upstream if in_degree[k] == 0)
        order: list[str] = []
        while ready:
            key = ready.popleft()
            order.append(key)
            for child in self.downstream[key]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    ready.append(child)
        if len(order) != len(self.upstream):
            raise CycleError(self._find_cycle(set(self.upstream) - set(order)))
        return tuple(order)

    def ancestors(self, key: str) -> frozenset[str]:
        return self._ancestors[key]

    @cached_property
    def _ancestors(self) -> Mapping[str, frozenset[str]]:
        result: dict[str, frozenset[str]] = {}
        for key in self.topological_order:  # parents are always computed first
            acc: set[str] = set()
            for dep in self.upstream[key]:
                acc.add(dep)
                acc |= result[dep]
            result[key] = frozenset(acc)
        return result

    # ------------------------------------------------------------------ #
    def _find_cycle(self, candidates: set[str]) -> list[str]:
        """Return one concrete cycle, e.g. ['a', 'b', 'c', 'a'], for a useful error message."""
        visiting: list[str] = []
        on_path: set[str] = set()
        done: set[str] = set()

        def dfs(node: str) -> list[str] | None:
            visiting.append(node)
            on_path.add(node)
            for dep in self.upstream[node]:
                if dep not in candidates or dep in done:
                    continue
                if dep in on_path:
                    start = visiting.index(dep)
                    return [*visiting[start:], dep]
                if (found := dfs(dep)) is not None:
                    return found
            on_path.discard(visiting.pop())
            done.add(node)
            return None

        for node in sorted(candidates):
            if node not in done and (cycle := dfs(node)) is not None:
                return list(reversed(cycle))  # present in dependency direction: a → b → a
        return sorted(candidates)  # unreachable for a genuinely cyclic graph
