# ADR-0001: PostgreSQL is the source of truth; the broker only carries IDs

- **Status:** Accepted
- **Date:** 2026-11-04

## Context

Celery can hold task arguments, results and retry state in Redis. Many hobby orchestrators let
Celery *be* the state machine. Redis is not durable by default, and its view of "what is running"
can disagree with reality after a worker crash or a broker restart.

## Decision

- Every workflow/task state lives in PostgreSQL and changes only through guarded transitions.
- Celery messages contain a single `task_run_id`. Workers load everything else from the DB.
- Celery result backend is **disabled** (`task_ignore_result=True`). Results go to `task_runs.output`.
- Workers claim work with compare-and-set (`UPDATE … WHERE status='queued'`).
- `acks_late=True` and `task_reject_on_worker_lost=True`, so a message is redelivered if a worker dies.

## Consequences

- ✅ Redis can be flushed at any time; the reaper rebuilds the queue from the DB.
- ✅ Duplicate deliveries are harmless.
- ✅ History, audit and dashboards query a single consistent store.
- ⚠️ Every task costs a few extra DB round-trips. That's negligible next to LLM/HTTP latency, and
  connection pooling keeps it cheap.
- ⚠️ Commit-then-enqueue leaves a small window where a commit succeeds and the enqueue fails. The reaper
  covers it. A transactional outbox table is the upgrade path if sub-second recovery is ever required.
