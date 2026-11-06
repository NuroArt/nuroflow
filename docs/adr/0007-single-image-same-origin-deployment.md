# ADR-0007: One backend image, same-origin edge, Compose as the reference deployment

- **Status:** Accepted
- **Date:** 2026-11-05

## Context

NuroFlow has four backend roles (API, worker, scheduler, migrations) and a static
dashboard. It must be deployable by a small team on one VM today, and portable to
Kubernetes or a managed container platform later, without a rewrite.

## Decision

1. **One backend image for all roles**, chosen by the container command. One artifact
   per release means the API and workers can never run different code against the same schema.
2. **Migrations are a one-shot job** that every other service waits for
   (`service_completed_successfully`). A Postgres advisory lock serializes concurrent migrators.
3. **One process per container, scale with replicas.** The API runs a single Uvicorn process
   (so `/metrics` describes one process) and scales horizontally. Celery uses prefork inside
   one container, with multiprocess metrics.
4. **Bulkheaded worker pools:** `default,notifications` and `ai` run as separate services, so
   slow LLM calls can never starve alerts.
5. **Same-origin edge:** nginx serves the SPA and proxies `/api`. This means no CORS, a strict CSP
   with no `unsafe-*`, and one place for body-size limits, header hygiene and SSE tuning.
   TLS terminates in front of it (Caddy profile or a cloud load balancer).
6. **Compose is the reference deployment.** Every hardening flag (read-only rootfs,
   `cap_drop: ALL`, `no-new-privileges`, non-root users, resource limits) maps one-to-one to a
   Kubernetes `securityContext`.

## Alternatives

| Option | Why not (now) |
|---|---|
| Image per role | More builds, and version skew between API and workers becomes possible |
| Migrations on API start-up | N replicas race; a failed migration crash-loops the API |
| Uvicorn `--workers N` | Per-process metrics would be scraped at random; replicas are the platform's job |
| SPA on a CDN, API on another origin | CORS plus cross-site tokens; a strict CSP is harder |
| Kubernetes manifests first | Most first deployments are one VM. Compose maps cleanly to Helm later |

## Consequences

- ✅ `make prod-up` deploys the full stack; CI runs the *same* stack and smoke test on every push.
- ✅ Horizontal scaling is `API_REPLICAS=4` / `--scale worker=3`.
- ⚠️ A single VM is a single point of failure. Postgres HA (managed Postgres or Patroni) is the
  first step past it; the app is already stateless.
