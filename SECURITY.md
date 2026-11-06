# Security

## Reporting a vulnerability

Please **don't open a public issue.** Email **security@nuroworks.com** with steps to reproduce.
You'll get an acknowledgement within 3 business days. Fixes for confirmed issues are prioritised
by severity, and you'll be credited unless you prefer otherwise.

## Threat model (summary)

| Asset | Threat | Controls |
|---|---|---|
| User accounts | credential stuffing, token theft | Argon2id; per-identity and per-IP login throttling; 15-min JWTs; rotating refresh tokens with **reuse detection → family revocation** |
| Third-party API keys (OpenAI, Anthropic, Telegram) | DB dump, insider, log leak | Fernet encryption at rest with **rotatable** keys; write-only over the API; never logged (`SecretStr`, masked `repr`, token-free error messages); owner-only access, no admin override |
| Workflow execution | code injection, prompt injection, SSRF | Declarative operators only, no user code (ADR-0004); sandboxed Jinja; model output never selects operators, connections or recipients; SSRF guard on outbound HTTP and webhooks |
| Inbound webhooks / events | forgery, replay, floods | HMAC-SHA256 with ±5-min window; delivery-ID dedupe; identical 401 for unknown/bad signature; rate limits; 256 KB edge limit |
| Platform | misconfiguration | Deployed environments **refuse to start** with dev secrets, a default DB password, an open Redis, derived encryption keys, `*` CORS, simulated integrations, private-network HTTP or an unauthenticated `/metrics` |
| Dashboard | XSS, clickjacking | Strict CSP (`script-src 'self'`, no `unsafe-*`), `frame-ancestors 'none'`, same-origin API, no tokens in URLs (SSE via `fetch` with headers) |
| Supply chain | malicious dependency or action | Locked dependencies (`uv.lock`, `package-lock.json`); pip-audit / npm audit / Trivy / gitleaks in CI; actions pinned to commit SHAs; SBOM + SLSA provenance on release images; weekly rebuild-and-rescan |
| Runtime | container breakout, tampering | Non-root users, read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`, code owned by root, only the edge publishes a port |

## Operational guidance

- Generate secrets with `make prod-secrets` and keep `.env.prod` out of git (it's ignored)
  and backed up. **Losing `NUROFLOW_ENCRYPTION_KEYS` means losing every stored credential.**
- Rotate encryption keys by **prepending** a new key (`new,old`), then re-encrypting. Never
  replace the list. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#rotating-secrets).
- Keep registration disabled in production and create users as an admin.
- Store third-party keys as *connections* (encrypted, per user), not in `.env` files, chat or notes.
- Set spending limits in every LLM provider console. `NuroFlowLLMSpendSpike` alerts are a
  second line, not the first.

## Accepted risks (documented, not ignored)

- **Refresh token in sessionStorage:** readable by XSS. Mitigated by the strict CSP and
  same-origin serving. Next step: an httpOnly cookie.
- **DNS rebinding** against the SSRF guard: check-then-connect has a window. Production
  egress through an allow-listing proxy closes it.
- **Dev-only npm advisories** (Vite/Vitest dev server): never shipped or exposed. CI gates on
  production dependencies, and Dependabot proposes the major upgrades.
