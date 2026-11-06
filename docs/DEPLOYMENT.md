# Deployment runbook

The reference deployment is the Compose stack in `docker-compose.yml` on one Linux VM
(2 vCPU / 4 GB is plenty to start). Design rationale: [ADR-0007](adr/0007-single-image-same-origin-deployment.md).

```
 internet ─▶ Caddy :443 (tls profile, auto-HTTPS) ─▶ web :8080 (nginx: SPA + /api proxy)
                                                         │
                       ┌──────────────── internal network ┴──────────────────────────┐
                       │ api ×2 ── postgres      worker (default,notifications)      │
                       │   │        ▲            worker-ai (ai)       scheduler ×2   │
                       │   └── redis (broker, password) ◀───────────────┘            │
                       │ migrate (one-shot)   prometheus/grafana (observability)     │
                       └──────────────────────────────────────────────────────────────┘
```

## 1. First deployment

```bash
# On the server (Docker 24+ with the compose plugin)
git clone <your fork> nuroflow && cd nuroflow
make prod-secrets domain=flow.example.com   # writes .env.prod (mode 600) with random secrets
nano .env.prod                              # set ACME_EMAIL; optional platform API keys
make prod-up                                # build → migrate → start; waits until healthy
make prod-admin email=you@example.com       # prompts for a password (12+ characters)
docker compose --env-file .env.prod --profile tls up -d caddy   # HTTPS via Let's Encrypt
```

DNS: point an `A` record for `flow.example.com` at the server before starting Caddy.
Firewall: allow 22, 80 and 443 only. Nothing else is published (the web container binds to
127.0.0.1:8080 unless you set `HTTP_BIND`).

**Back up `.env.prod` now** (password manager or secrets vault). Without
`NUROFLOW_ENCRYPTION_KEYS`, stored credentials can't be recovered.

Verify:

```bash
make prod-ps                                                 # every service "healthy"
BASE_URL=https://flow.example.com SMOKE_EMAIL=you@example.com SMOKE_PASSWORD=... make smoke
```

The smoke test checks 18 things through the public edge: security headers and CSP, that
`/metrics` is not exposed, that registration is disabled, login, a real run executed by the
workers, SSE passing through unbuffered, logs and stats.

## 2. Going live with AI and Telegram

Keys go into **connections** (encrypted at rest, write-only, per user). Not into `.env` files,
chat messages or notes. In the dashboard or via the API:

```bash
API=https://flow.example.com/api/v1
TOKEN=$(curl -s $API/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"…"}' | jq -r .access_token)

# Paste each key from your password manager at the prompt (keeps it out of shell history)
read -rs KEY && curl -s $API/connections -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "{\"name\":\"anthropic\",\"type\":\"anthropic\",\"secret\":\"$KEY\"}"; unset KEY
curl -s -X POST $API/connections/<id>:test -H "Authorization: Bearer $TOKEN"   # proves the key works
```

Repeat for `openai` and your Telegram bot (`type: telegram`). Then message your bot and call
`GET /connections/{id}/telegram-chats` to find your chat ID. Set **spending limits in each
provider's console**, and optionally `NUROFLOW_LLM_PRICING` for cost tracking. Full guide:
[INTEGRATIONS.md](INTEGRATIONS.md#going-live-checklist).

## 3. Upgrades

```bash
git pull                     # or bump NUROFLOW_TAG to a released image version
make prod-up                 # rebuilds, runs `migrate`, then recreates changed services
```

- Migrations follow **expand → migrate → contract** (see DATABASE.md): every release's schema
  works with the previous release's code, so rolling restarts never break running workers.
- Workers get `stop_grace_period` (2 min, or 5 min for AI) to finish in-flight tasks. Anything
  interrupted is redelivered (acks_late) or reaped and retried, because Postgres is the source
  of truth (ADR-0001).
- **Rollback:** redeploy the previous image tag. Thanks to expand/contract the old code runs
  on the new schema. Only roll a migration back (`alembic downgrade -1`) if the release notes
  say it's safe.

Using released images instead of building on the server:

```bash
echo 'NUROFLOW_IMAGE=ghcr.io/<owner>/nuroflow-backend' >> .env.prod
echo 'NUROFLOW_WEB_IMAGE=ghcr.io/<owner>/nuroflow-web'  >> .env.prod
NUROFLOW_TAG=1.0.0 docker compose --env-file .env.prod pull
NUROFLOW_TAG=1.0.0 docker compose --env-file .env.prod up -d --wait
gh attestation verify oci://ghcr.io/<owner>/nuroflow-backend:1.0.0 -o <owner>   # provenance
```

## 4. Scaling

| Pressure | Signal | Action |
|---|---|---|
| API latency | `nuroflow_http_request_duration_seconds` p95 | `API_REPLICAS=4 make prod-up` |
| Task backlog | `nuroflow_oldest_queued_task_age_seconds`, `nuroflow_queue_depth{queue}` | `docker compose … up -d --scale worker=3` (or `worker-ai`) |
| LLM rate limits | 429s in task attempts | fewer `worker-ai` processes (`-c`), not more |
| DB connections | Postgres `max_connections` | each process uses `NUROFLOW_DATABASE_POOL_SIZE`; add PgBouncer past ~200 |

Schedulers don't need scaling for throughput. Two replicas exist for availability (ADR-0002).

## 5. Backups and restore

Postgres holds **everything** (Redis is only a broker). Nightly logical backup:

```bash
docker compose --env-file .env.prod exec -T postgres \
  pg_dump -U nuroflow -d nuroflow --format=custom > backups/nuroflow-$(date +%F).dump
```

Ship the dumps off the server and encrypt them. Test a restore regularly:

```bash
docker compose --env-file .env.prod exec -T postgres \
  pg_restore -U nuroflow -d nuroflow --clean --if-exists < backups/nuroflow-2026-11-05.dump
```

A backup is useless without `.env.prod`: encrypted columns need `NUROFLOW_ENCRYPTION_KEYS`.
For point-in-time recovery, use managed Postgres (or WAL archiving with pgBackRest).

## 6. Rotating secrets

| Secret | How | Impact |
|---|---|---|
| `NUROFLOW_ENCRYPTION_KEYS` | prepend a new key (`new,old`) → deploy → `docker compose … run --rm api nuroflow rotate-encryption` → remove `old` → deploy | none (tested) |
| `NUROFLOW_SECRET_KEY` | replace → deploy | all sessions end and API keys stop verifying (HMAC pepper): reissue API keys |
| `NUROFLOW_METRICS_TOKEN` | replace → deploy (Prometheus reads the same env) | none |
| `POSTGRES_PASSWORD` | `ALTER ROLE nuroflow PASSWORD '…'` → update `.env.prod` → deploy | brief reconnects |
| `REDIS_PASSWORD` | update `.env.prod` → `up -d` (recreates redis and clients) | queued messages survive (AOF); anything lost is re-enqueued by the reaper |
| Webhook secrets | `POST /triggers/{id}:rotate-secret` | update the sender |
| Provider keys | `PATCH /connections/{id}` with the new secret, then `:test` | none |

## 7. Monitoring

```bash
docker compose --env-file .env.prod --profile observability up -d
ssh -L 3000:127.0.0.1:3000 you@server     # Grafana on your laptop: http://localhost:3000
```

Prometheus discovers every API replica, worker pool and scheduler via DNS and authenticates to
the API with the metrics token. Wire `deploy/prometheus/alerts.yml` to Alertmanager (or Grafana
alerting) and a pager. See [OBSERVABILITY.md](OBSERVABILITY.md).

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| A service is "unhealthy" | `docker compose … logs <service>`; `docker compose … exec <service> nuroflow healthcheck <role>` |
| App refuses to start: "unsafe configuration for production" | the message lists each problem (default passwords, missing keys…) |
| `migrate` failed | `docker compose … logs migrate`; the lock timeout prevents waiting forever behind long queries |
| Runs stuck in `pending` | `admitted_at` null means `max_active_runs` is reached (expected backpressure); otherwise check worker health |
| Runs stuck in `queued` | worker down, or the queue has no consumer (`worker-ai` for `ai`) |
| Live views don't update | a proxy in front of nginx is buffering. Disable buffering for `text/event-stream` |
| `401` on `/metrics` | the scraper must send `Authorization: Bearer $NUROFLOW_METRICS_TOKEN` |
