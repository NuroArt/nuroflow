# ADR-0002: Custom DB-driven scheduler instead of Celery beat

- **Status:** Accepted
- **Date:** 2026-11-04

## Context

Cron triggers are created, edited and paused at runtime through the API. Celery beat reads its
schedule from static config (or third-party DB schedulers such as `django-celery-beat` and `redbeat`).
Beat must also run as a **single** instance, or every job double-fires.

## Decision

`nuroflow-scheduler` is a small asyncio service. On each tick it:

1. `SELECT … FROM workflow_triggers WHERE type='cron' AND is_enabled AND next_fire_at <= now()
   ORDER BY next_fire_at LIMIT :batch FOR UPDATE SKIP LOCKED`
2. Inserts a `workflow_run` with `idempotency_key = 'cron:{trigger_id}:{scheduled_for}'`
   (unique index, `ON CONFLICT DO NOTHING`).
3. Computes the next `next_fire_at` with `croniter` in the trigger's timezone, applying the
   `catchup` policy.
4. Commits, then enqueues the run's root tasks.

The same loop also dispatches due retries, reaps stale tasks and enforces run timeouts.

## Alternatives

| Option | Why rejected |
|---|---|
| Celery beat + static schedule | Not editable at runtime |
| `celery-redbeat` | State lives in Redis, which contradicts ADR-0001 |
| APScheduler | The default job store is in-memory; its SQLAlchemy store adds a second scheduling model |
| Postgres `pg_cron` | Ties scheduling to a DB extension, which managed Postgres often doesn't allow |

## Consequences

- ✅ Multiple scheduler replicas can run safely (HA) with no leader election.
- ✅ Schedule changes take effect on the next tick (≤ 5s).
- ⚠️ We own about 300 lines of scheduling code, so it needs thorough tests (DST transitions, catchup, races).

## Addendum (Phase 4 implementation)

- **Sync loop, not asyncio.** The jobs are sequential short transactions with nothing to
  overlap, so an event loop added complexity with no throughput gain.
- **Lock ordering refined.** Locking `workflow_triggers` directly (as sketched above) would
  invert the order used when saving a version (workflow → trigger) and could deadlock. Due
  triggers are now found with an unlocked read, then locked as workflow → trigger, each
  with `SKIP LOCKED`, and re-checked. See SCHEDULER.md for the global lock order.
- **Admission control** was added: automated runs over `max_active_runs` wait instead of
  being rejected.
