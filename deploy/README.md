# Deployment assets

| Path | What |
|---|---|
| `../docker-compose.yml` | production stack: postgres, redis, migrate, api ×2, worker pools, scheduler ×2, web; `tls` and `observability` profiles |
| `../backend/Dockerfile`, `../frontend/Dockerfile` (+ `nginx.conf`) | hardened multi-stage images (non-root, read-only, tini, health checks) |
| `generate-secrets.py` | writes `.env.prod` with fresh secrets (`make prod-secrets`) |
| `smoke.py` | 18-check end-to-end test through the public edge (`make smoke`; also run in CI) |
| `caddy/Caddyfile` | automatic HTTPS in front of `web` |
| `prometheus/` | dev and in-stack scrape configs, plus `alerts.yml` (7 rules) |
| `grafana/` | provisioned datasource and the NuroFlow Overview dashboard |

Runbook: [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md). Dev dependencies only: [docker-compose.dev.yml](../docker-compose.dev.yml).
