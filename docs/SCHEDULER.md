# NuroFlow — Scheduler, Triggers & Recovery

Phase 4 · code in `scheduler/`, `domain/schedule.py`, `services/ingest.py`, `services/launcher.py`

## Four ways to start a run

| Trigger | Declared in spec | Auth | When the workflow is full (`max_active_runs`) |
|---|---|---|---|
| **Manual** `POST /workflows/{id}/runs` | n/a | JWT / API key (`runs:write`) | **409**: a human is waiting for an answer |
| **Cron** | `{type: cron, cron, timezone, catchup}` | n/a | **Queued** (waits for a slot) |
| **Event** `POST /events` | `{type: event, event_type, event_filter}` | API key (`events:write`) | **Queued** |
| **Webhook** `POST /hooks/{trigger_id}` | `{type: webhook, name}` | HMAC signature | **Queued** |

Triggers are part of the workflow spec. Saving a version **syncs** them by identity:

| Trigger | Identity (`spec_key`) | Kept across versions |
|---|---|---|
| cron | expression + timezone | position in time (`next_fire_at`); `catchup` can change freely |
| event | event_type + filter | n/a |
| webhook | `name` | URL **and signing secret**. Integrations keep working when you edit the workflow |

## Backpressure, not rejection

Automated runs beyond `max_active_runs` are **persisted but waiting** (`admitted_at = null`,
no task queued). Each scheduler tick admits waiting runs **oldest first** as slots free up.
A burst of 500 leads gets processed, never dropped. The run's `timeout_seconds` clock
starts at admission, not while waiting.

## The scheduler loop

```bash
nuroflow-scheduler          # or: make scheduler. Run 2+ replicas for HA.
```

Every tick (default 2s) runs five jobs in order. Each job dispatches only after its own commit.

| # | Job | What it does | Query |
|---|---|---|---|
| 1 | `expire_runs` | Fail runs past `deadline_at`, cancel their open tasks | `ix_workflow_runs_deadline_active` |
| 2 | `reap_stale_tasks` | **Lost message** (queued > 10 min) → re-enqueue. **Dead worker** (no heartbeat 90s) → attempt `WorkerLostError`, retry now or fail | `ix_task_runs_queued_since`, `ix_task_runs_in_flight_heartbeat` |
| 3 | `dispatch_retries` | `up_for_retry` with elapsed backoff → `queued` | `ix_task_runs_retry_due` |
| 4 | `fire_cron_triggers` | Create runs for due slots, advance `next_fire_at` | `ix_workflow_triggers_due_cron` |
| 5 | `admit_waiting_runs` | Give free slots to waiting runs | `ix_workflow_runs_waiting` |

Order matters: the reaper turns a dead worker's task into a retry, and job 3 dispatches it
in the **same tick**. A job that throws is logged and skipped; the others still run.

### High availability without leader election

- Candidates are found with an **unlocked** read, then locked one by one with
  `FOR UPDATE SKIP LOCKED` and **re-checked**. Two schedulers split the work instead of
  blocking or double-processing (tested: two schedulers racing over 15 due triggers
  produce exactly 15 runs).
- Every cron run has `idempotency_key = cron:{trigger}:{slot}` (unique index), a second
  line of defence against duplicates.
- **Global lock order: workflow → trigger → run → task.** Every code path (API launches,
  version saves, cron, admission, executor, reaper, cancel, timeouts) acquires locks in
  this order, so there's no deadlock cycle.

Why a plain synchronous loop and not asyncio? Each job is a handful of short SQL
transactions run in sequence, with no concurrent I/O to overlap. Scale-out is
horizontal (more replicas), not more coroutines.

## Cron and daylight saving

Schedules are evaluated in the trigger's IANA timezone with Vixie-cron semantics:

| Case (America/New_York) | Naive croniter | NuroFlow |
|---|---|---|
| `30 1 * * *` on fall-back day (01:30 happens twice) | fires **twice** | fires **once** |
| `0 * * * *` on fall-back day | 01:00 EDT and 01:00 EST | same: two genuinely different hours |
| `30 2 * * *` on spring-forward day (02:30 doesn't exist) | 03:00 | 03:00 (runs late, never skipped) |

**Catchup.** After downtime, `catchup: false` (default) fires **once** for the most recent
missed slot. `catchup: true` backfills every missed slot (bounded per tick by
`NUROFLOW_CRON_MAX_CATCHUP_RUNS`), and admission paces them through `max_active_runs`.

## Events

```bash
curl -X POST $API/api/v1/events \
  -H "X-API-Key: $KEY" -H "Idempotency-Key: typeform-response-77" \
  -H "Content-Type: application/json" \
  -d '{"type": "lead.captured", "source": "website-form",
       "payload": {"name": "Ada", "email": "ada@example.com", "source": "website"}}'
# 202 {"event_id": "…", "duplicate": false, "runs": [{…, "admitted_at": "…"}]}
```

- **Fan-out:** every enabled event trigger with that `event_type` across your workflows.
- **Filters** use Postgres JSONB containment (`payload @> event_filter`).
  `{source: website}` matches any payload containing that key/value.
- **Idempotency:** the same `Idempotency-Key` returns the original event and its runs
  (`200`, `duplicate: true`), so client retries never create duplicate leads.
- Rate limited per API key (`NUROFLOW_EVENT_RATE_LIMIT_PER_MINUTE`).

## Webhooks

Get the URL and secret (interactive login only; API keys can't read secrets):

```bash
curl $API/api/v1/workflows/$WF/triggers -H "Authorization: Bearer $TOKEN"
curl $API/api/v1/triggers/$TRIGGER/secret -H "Authorization: Bearer $TOKEN"
curl -X POST $API/api/v1/triggers/$TRIGGER:rotate-secret -H "Authorization: Bearer $TOKEN"
```

Senders sign `"{timestamp}.{raw body}"` with HMAC-SHA256:

```bash
# shell
TS=$(date +%s); BODY='{"event":"invoice.paid"}'
SIG=sha256=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex | awk '{print $NF}')
curl -X POST "$WEBHOOK_URL" -H "X-NuroFlow-Timestamp: $TS" -H "X-NuroFlow-Signature: $SIG" \
     -H "X-NuroFlow-Delivery: evt_123" -H "Content-Type: application/json" -d "$BODY"
```

```python
# python
import hashlib, hmac, json, time, httpx
body = json.dumps(payload).encode(); ts = str(int(time.time()))
sig = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
httpx.post(url, content=body, headers={"X-NuroFlow-Timestamp": ts, "X-NuroFlow-Signature": sig,
                                       "Content-Type": "application/json"})
```

| Protection | How |
|---|---|
| Forgery | HMAC-SHA256 with a per-trigger secret, encrypted at rest (Fernet, rotatable keys) |
| Replay | Signed timestamp must be within ±5 min (`NUROFLOW_WEBHOOK_TOLERANCE_SECONDS`) |
| Duplicate deliveries | Optional `X-NuroFlow-Delivery` → same run returned (`200`, `duplicate: true`) |
| Enumeration | Unknown trigger and bad signature give the identical `401` |
| Timing attacks | `hmac.compare_digest` |
| Flooding | Per-trigger rate limit (`NUROFLOW_WEBHOOK_RATE_LIMIT_PER_MINUTE`), 256 KB body cap |

## Proven failure scenarios (tests/integration/test_scheduler.py)

| Chaos | Outcome |
|---|---|
| Worker OOM-killed mid-task | Reaped after 90s silence → attempt 1 `WorkerLostError` → attempt 2 succeeds → run **succeeded** |
| Dead worker, no retries left | Task failed, downstream skipped, run failed, with the reason recorded |
| Redis flushed (queued message lost) | Re-enqueued after 10 min → run **succeeded** |
| "Lost" message wasn't lost (duplicate delivery) | CAS claim → executed exactly once |
| Two schedulers racing | Each cron fires exactly once |
| Run exceeds `timeout_seconds` | Run failed, open tasks cancelled, late messages refused |
| A scheduler job crashes | Logged; the other four jobs still run that tick |

## Tuning

| Setting | Default | Notes |
|---|---|---|
| `NUROFLOW_SCHEDULER_TICK_SECONDS` | 2 | Cron precision and retry latency floor |
| `NUROFLOW_REAPER_HEARTBEAT_TIMEOUT_SECONDS` | 90 | Must be ≥ 3× heartbeat interval (validated at startup) |
| `NUROFLOW_REAPER_QUEUED_TIMEOUT_SECONDS` | 600 | Above your normal queue wait, or busy queues get duplicate (harmless) messages |
| `NUROFLOW_CRON_MAX_CATCHUP_RUNS` | 50 | Per trigger per tick |
| `NUROFLOW_PUBLIC_BASE_URL` | http://localhost:8000 | Used in webhook URLs shown to users |
