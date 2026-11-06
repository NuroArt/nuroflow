"""Prometheus metric definitions: one module, so names and labels stay consistent.

Naming follows Prometheus conventions: ``nuroflow_`` prefix, base units (seconds,
not ms), ``_total`` for counters.

**Label cardinality is a security and cost concern.** Every distinct label value is
a new time series in Prometheus. So:

* HTTP metrics use the *route template* (``/api/v1/runs/{run_id}``), never the raw
  path. Unmatched paths collapse into ``route="unmatched"``, so a scanner probing
  random URLs can't create millions of series.
* No user-controlled free text (workflow names, task keys, error messages) is ever
  a label. ``operator`` and ``queue`` come from a fixed set; LLM ``model`` comes
  from workflow YAML, but it's bounded in practice and essential for cost tracking.

Gauges declare ``multiprocess_mode`` so they aggregate correctly when Celery prefork
children write them (see ``servers.py``). It's ignored in single-process mode.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ---- HTTP (API) -------------------------------------------------------------- #
HTTP_REQUESTS = Counter(
    "nuroflow_http_requests_total", "HTTP requests handled", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "nuroflow_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

# ---- Execution (worker) ------------------------------------------------------ #
TASK_ATTEMPTS = Counter(
    "nuroflow_task_attempts_total",
    "Task attempts by final outcome of the attempt",
    ["operator", "outcome"],  # succeeded | retrying | failed
)
TASK_DURATION = Histogram(
    "nuroflow_task_duration_seconds",
    "Operator execution time per attempt",
    ["operator"],
    # From a 5 ms noop to a slow LLM call.
    buckets=(0.005, 0.025, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)
RUNS_FINISHED = Counter(
    "nuroflow_runs_finished_total", "Workflow runs reaching a terminal state", ["status"]
)
LLM_TOKENS = Counter(
    "nuroflow_llm_tokens_total", "LLM tokens consumed", ["provider", "model", "kind"]
)
LLM_COST = Counter(
    "nuroflow_llm_cost_usd_total", "Estimated LLM spend (configured prices)", ["provider", "model"]
)
NOTIFICATIONS = Counter(
    "nuroflow_notifications_total", "Notification send attempts", ["channel_type", "outcome"]
)

# ---- Scheduler --------------------------------------------------------------- #
SCHEDULER_TICK = Histogram(
    "nuroflow_scheduler_tick_duration_seconds",
    "Duration of one scheduler tick (all jobs)",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
SCHEDULER_DISPATCHED = Counter(
    "nuroflow_scheduler_dispatched_total", "Task runs dispatched by scheduler jobs", ["job"]
)
SCHEDULER_JOB_ERRORS = Counter(
    "nuroflow_scheduler_job_errors_total", "Scheduler job failures", ["job"]
)
SCHEDULER_LAST_TICK = Gauge(
    "nuroflow_scheduler_last_tick_timestamp_seconds",
    "Unix time of the last completed tick (alert if stale)",
    multiprocess_mode="max",
)

# ---- System state (computed from Postgres on scrape, see db_gauges.py) ------- #
QUEUE_DEPTH = Gauge(
    "nuroflow_queue_depth",
    "Task runs queued and not yet claimed",
    ["queue"],
    multiprocess_mode="mostrecent",
)
OLDEST_QUEUED = Gauge(
    "nuroflow_oldest_queued_task_age_seconds",
    "Age of the oldest queued task (workers can't keep up when this grows)",
    multiprocess_mode="mostrecent",
)
ACTIVE_RUNS = Gauge(
    "nuroflow_active_runs",
    "Non-terminal runs by status",
    ["status"],
    multiprocess_mode="mostrecent",
)
SCHEDULER_LAG = Gauge(
    "nuroflow_scheduler_lag_seconds",
    "How overdue the most overdue enabled cron trigger is (0 when on time)",
    multiprocess_mode="mostrecent",
)
NOTIFICATIONS_PENDING = Gauge(
    "nuroflow_notifications_pending", "Undelivered outbox rows", multiprocess_mode="mostrecent"
)
