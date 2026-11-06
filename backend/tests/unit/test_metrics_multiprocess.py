"""Regression: worker metrics must use multiprocess storage, or Celery children's
counters are invisible. Each check runs in a fresh interpreter, because the bug
is about *import order* and this test process has already imported everything."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def run(code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_worker_cli_module_does_not_import_prometheus_client() -> None:
    out = run(
        """
        import sys
        import nuroflow.workers.cli, nuroflow.observability.multiproc
        print("prometheus_client" in sys.modules)
        """
    )
    assert out == "False"


def test_children_write_samples_the_parent_can_aggregate(tmp_path: Path) -> None:
    out = run(
        f"""
        import os
        os.environ["PROMETHEUS_MULTIPROC_DIR"] = {str(tmp_path / "m")!r}
        from nuroflow.observability.multiproc import prepare_multiprocess_dir
        prepare_multiprocess_dir()
        from nuroflow.observability import metrics
        if os.fork() == 0:  # a prefork child records an attempt, then exits
            metrics.TASK_ATTEMPTS.labels(operator="noop", outcome="succeeded").inc(3)
            os._exit(0)
        os.wait()
        from prometheus_client import CollectorRegistry, generate_latest, multiprocess
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        print(generate_latest(registry).decode())
        """
    )
    assert 'nuroflow_task_attempts_total{operator="noop",outcome="succeeded"} 3.0' in out
