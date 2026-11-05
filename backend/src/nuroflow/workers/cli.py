"""``nuroflow-worker`` console script: a Celery worker with NuroFlow defaults.

Extra arguments pass straight through to Celery, e.g. ``nuroflow-worker -Q ai -c 2``.
"""

from __future__ import annotations

import sys

from nuroflow.core.config import get_settings
from nuroflow.core.logging import configure_logging


def run() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    from nuroflow.workers.celery_app import celery_app

    argv = ["worker", f"--loglevel={settings.log_level}"]
    passthrough = sys.argv[1:]
    if not any(a in ("-Q", "--queues") or a.startswith("--queues=") for a in passthrough):
        argv += ["-Q", ",".join(settings.worker_queues)]
    celery_app.worker_main([*argv, *passthrough])


if __name__ == "__main__":
    run()
