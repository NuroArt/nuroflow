"""Multiprocess-mode setup that must run **before** ``prometheus_client`` is imported.

``prometheus_client`` picks its storage backend once, at import time: if
``PROMETHEUS_MULTIPROC_DIR`` isn't set yet, every process counts in private memory and
the worker's metrics endpoint silently reports nothing from its Celery children. This
module therefore imports nothing from ``prometheus_client`` (a test enforces that).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ENV = "PROMETHEUS_MULTIPROC_DIR"


def prepare_multiprocess_dir() -> Path:
    """Use ``$PROMETHEUS_MULTIPROC_DIR`` or a fresh temp dir. Stale files from a previous
    run would be summed into the new one, so the directory is always emptied first."""
    if "prometheus_client" in sys.modules and not os.environ.get(ENV):
        raise RuntimeError("prometheus_client was imported before multiprocess mode was set up")
    path = Path(os.environ.get(ENV) or tempfile.mkdtemp(prefix="nuroflow-metrics-"))
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    os.environ[ENV] = str(path)
    return path
