"""In-process dispatcher: executes tasks synchronously, breadth-first.

Used by the end-to-end tests and ``nuroflow run-local``. It drives the *exact same*
executor, orchestrator and SQL as production. Only the transport differs (a deque
instead of Redis), so an e2e test passing here exercises the real engine.

It drains with a loop rather than recursion, so a 200-task DAG can't blow the stack.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence

from nuroflow.engine.dispatch import EnqueueRequest
from nuroflow.engine.executor import TaskExecutor


class InlineDispatcher:
    def __init__(self, executor: TaskExecutor | None = None, *, max_steps: int = 10_000) -> None:
        self.executor = executor
        self.max_steps = max_steps
        self.history: list[EnqueueRequest] = []
        self._pending: deque[EnqueueRequest] = deque()
        self._draining = False

    def enqueue(self, requests: Sequence[EnqueueRequest]) -> None:
        self._pending.extend(requests)
        self.history.extend(requests)
        if self._draining or self.executor is None:
            return
        self._draining = True
        try:
            steps = 0
            while self._pending:
                steps += 1
                if steps > self.max_steps:
                    raise RuntimeError("inline dispatcher exceeded max_steps (runaway workflow?)")
                request = self._pending.popleft()
                follow_ups = self.executor.execute(request.task_run_id)
                self._pending.extend(follow_ups)
                self.history.extend(follow_ups)
        finally:
            self._draining = False
