# ADR-0003: Engine-managed retries

- **Status:** Accepted
- **Date:** 2026-11-04

## Context

Celery offers `autoretry_for` and `self.retry(countdown=…)`. With a Redis broker, delayed (ETA)
messages are prefetched into worker memory until they become due. If they wait longer than the
broker's `visibility_timeout`, they are redelivered (duplicates). If the worker restarts, they are
lost or reordered. Retry history also isn't visible anywhere durable.

## Decision

- Operators signal intent through exception types: `RetryableError` (timeouts, 429, 5xx,
  connection errors) vs `NonRetryableError` (validation, 4xx, schema mismatch). Unknown
  exceptions count as retryable.
- The Executor records a `task_attempts` row for every try.
- `RetryPolicy` (per task: `retries`, `retry_backoff`, `retry_backoff_max`, `retry_jitter`)
  computes `next_attempt_at` using exponential backoff with full jitter.
- The task moves to `up_for_retry`. The scheduler re-enqueues it when it's due.

## Consequences

- ✅ Retries survive restarts and are visible in the UI ("attempt 2 of 4, next in 40s").
- ✅ One retry implementation for every operator.
- ⚠️ Retry latency is bounded below by the scheduler tick (default 5s). That's fine for business
  automations; sub-second retries aren't a goal.
