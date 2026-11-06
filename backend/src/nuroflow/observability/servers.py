"""Metric endpoints for the non-HTTP processes (worker, scheduler).

Celery prefork forks N children. Each child has its own copy of every counter, so a
plain ``start_http_server`` in the parent would always report zeros. Prometheus'
**multiprocess mode** solves this: every process writes its samples to mmap files in
``PROMETHEUS_MULTIPROC_DIR``, and the parent serves the aggregate of all of them.

The directory must be set *before* ``prometheus_client`` creates any metric, which is
why the worker CLI calls ``multiproc.prepare_multiprocess_dir`` before importing this module.
"""

from __future__ import annotations

import logging
import os

from prometheus_client import CollectorRegistry, multiprocess, start_http_server

from nuroflow.observability.multiproc import ENV

logger = logging.getLogger(__name__)


def serve_multiprocess(port: int, addr: str = "0.0.0.0") -> None:  # noqa: S104 (scrape target)
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
    start_http_server(port, addr=addr, registry=registry)
    logger.info("metrics server started", extra={"port": port, "mode": "multiprocess"})


def serve_single_process(port: int, addr: str = "0.0.0.0") -> None:  # noqa: S104
    start_http_server(port, addr=addr)
    logger.info("metrics server started", extra={"port": port, "mode": "single"})


def mark_child_dead(pid: int) -> None:
    """Called when a prefork child exits, so its live gauges stop being reported."""
    if os.environ.get(ENV):
        multiprocess.mark_process_dead(pid)  # type: ignore[no-untyped-call]
