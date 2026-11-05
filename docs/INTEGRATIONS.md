# Integrations: LLMs, Telegram and notifications (Phase 5)

This phase connects NuroFlow to the outside world: OpenAI, Anthropic, Telegram and
outbound webhooks. Three ideas shape everything here:

1. **Ports and adapters.** Operators depend on `LLMProvider` and `TelegramClient` Protocols.
   The real adapters, the deterministic fakes and the test doubles are interchangeable.
2. **Credentials are resolved per call, never stored on adapters.** One adapter instance
   serves every user, each with their own key.
3. **Side effects go through a transactional outbox**, so an alert exists if and only if the
   state change that caused it committed.

```
                 ┌────────────── worker ───────────────┐
 task: ai.structured ─▶ AIOperator ─▶ CredentialResolver ─▶ connections (Fernet)
                        │   │                          └▶ platform default (settings)
                        │   └▶ provider.complete(request, api_key=…)  ─▶ OpenAI / Anthropic
                        └▶ metrics.llm {tokens, cost_usd, fallback_index} ─▶ task_attempts

 engine transition ─(same txn)─▶ notification_deliveries(pending)
 scheduler: claim due rows (lease) ─▶ Celery "nuroflow.deliver_notification" ─▶ Telegram / webhook
```

## Modes: live vs simulated

| Setting | `simulated` | `live` |
|---|---|---|
| `NUROFLOW_LLM_MODE` | every provider name maps to `FakeLLMProvider` (schema-valid JSON, $0) | real OpenAI/Anthropic adapters; `fake` still available |
| `NUROFLOW_TELEGRAM_MODE` | `FakeTelegramClient` records messages | Telegram Bot API |

`.env.example` ships `simulated`, so the reference workflow runs with zero keys.
**Staging and production refuse to start in simulated mode**: a silently faked alert
in production is worse than a crash at boot.

## Credentials

### Resolution order (most specific first)

1. `connection: <name>` on the task → the **workflow owner's** connection. Ownership is a
   SQL `WHERE owner_id = …`, so a workflow can never use another user's key.
2. Platform default from settings (`NUROFLOW_OPENAI_API_KEY`, `NUROFLOW_ANTHROPIC_API_KEY`,
   `NUROFLOW_TELEGRAM_BOT_TOKEN`).
3. Otherwise the task fails **permanently** with a message saying exactly how to fix it.
   Retrying a missing key only wastes time.

### The connections API

```bash
# Store a key. Encrypted with Fernet before INSERT, never returned (responses say has_secret: true).
curl -X POST $API/connections -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name": "sales-bot", "type": "telegram", "secret": "123456:AA…"}'

curl -X POST $API/connections/$ID:test   …   # {"ok": true, "detail": "connected as @my_bot"}
curl $API/connections/$ID/telegram-chats   …   # message your bot first, then find your chat_id
curl -X PATCH $API/connections/$ID … -d '{"secret": "<new>"}'   # rotate; old value is overwritten
```

| Type | Secret | Allowed `config` |
|---|---|---|
| `openai` | API key | `base_url` (https only: the key is sent there), `test_model` |
| `anthropic` | API key | `base_url`, `test_model` |
| `telegram` | Bot token | none |
| `http` | Signing secret for webhook channels | none |

Security properties, each covered by a test:

- The raw column holds ciphertext. The test reads it with plain SQL and checks the token
  isn't there.
- Connections are owner-only, **with no admin override**: they hold someone's money.
- Writing needs the `connections:write` scope (editors have it, viewers don't), separate
  from `workflows:write`, so a narrowly scoped automation key can't swap credentials.
- Secrets use `SecretStr` in request schemas, so they never appear in validation errors.
  `Credential.__repr__` masks them, and Telegram errors never include the URL (the token
  is part of the URL path).
- A connection used by a channel can't be deleted (409), so alerts never silently stop.

## LLM adapters

Both use plain `httpx` rather than vendor SDKs. That gives one timeout and error policy for
every integration, trivial testing with `httpx.MockTransport`, no SDK churn, and support for any
OpenAI-compatible server through `base_url`.

| | OpenAI | Anthropic |
|---|---|---|
| Endpoint | `POST /chat/completions` | `POST /messages` (`anthropic-version: 2023-06-01`) |
| Auth | `Authorization: Bearer` | `x-api-key` |
| Structured output | `response_format: json_schema` | forced tool call (`tool_choice`) whose `input_schema` is your schema |
| Tokens | `usage.prompt_tokens / completion_tokens` | `usage.input_tokens / output_tokens` |

Either way, `ai.structured` **still validates** the answer against your schema afterwards
(defence in depth). A malformed answer is retryable, because the next sample is usually fine.

### Error classification (`integrations/http_errors.py`)

| Outcome | Retryable | Why |
|---|---|---|
| timeout, connection error | ✅ | network blips heal |
| 408, 409, 425, 429 | ✅ | the provider asked us to slow down |
| 5xx, including Anthropic 529 *overloaded* | ✅ | provider-side incident |
| other 4xx (bad key, bad request, model refusal) | ❌ | retrying a bug burns money |

### Failover

```yaml
- key: qualify
  operator: ai.structured
  inputs:
    provider: anthropic
    model: claude-sonnet-4-5
    connection: team-claude          # optional
    fallback:
      - { provider: openai, model: gpt-4o-mini }
```

Targets are tried in order, but **only on retryable errors**. A 401 on the primary fails the
task immediately. Failing over would hide a broken key behind a different, working model, and
you'd find out from the invoice. Engine retries (ADR-0003) still apply on top: failover handles
the outage *now*, retries handle it *later*.

### Cost tracking

Every call records `metrics.llm = {provider, model, input_tokens, output_tokens, fallback_index, cost_usd?}`
on its attempt row. Prices are **configuration, not code**, because they change:

```bash
NUROFLOW_LLM_PRICING='{"gpt-4o-mini": {"input": 0.15, "output": 0.60}, "claude-sonnet-4-5": {"input": 3, "output": 15}}'
```

The longest matching model prefix wins (`gpt-4o-mini` matches `gpt-4o-mini-2024-07-18` ahead of
`gpt-4o`). Unpriced models still record tokens, and `GET /usage/llm?days=30` reports them as
`unpriced_calls`, so a missing price is visible instead of silently costing $0. The report is one
`GROUP BY` over JSONB in Postgres.

## Notifications

### Wiring

```bash
curl -X POST $API/channels … -d '{"name": "ops-alerts", "type": "telegram", "target": "-1001234567890", "connection": "sales-bot"}'
curl -X POST $API/channels/$ID:test …        # sends a real test message now
```

```yaml
# in the workflow
notifications:
  - channel: ops-alerts
    events: [run.failed, task.retrying]   # also: run.started, run.succeeded
```

Channels are referenced **by name**, so the same YAML works in every environment. Saving a
version with an unknown channel name is a 422. Without that check, a typo would mean alerts
that never arrive and nobody noticing until the outage. Subscriptions are synced from the spec
on every version save, just like triggers.

Webhook channels `POST` the JSON payload to an `https` URL. Attach an `http` connection and every
delivery is signed with the same scheme as inbound webhooks (`X-NuroFlow-Timestamp`,
`X-NuroFlow-Signature: sha256=HMAC(secret, "{ts}.{body}")`), and the SSRF guard from
`http.request` applies.

### The outbox, step by step

| Step | Where | What |
|---|---|---|
| **Emit** | the engine transaction that changed state | `emit()` inserts one `pending` row per subscribed, enabled channel, with a payload snapshot |
| **Claim** | scheduler job `deliver_notifications` | `FOR UPDATE SKIP LOCKED` on due rows; `attempt_count += 1`; `next_attempt_at = now + 2 min` (the lease) |
| **Send** | Celery task on the `notifications` queue | load the channel, decrypt credentials, send with **no transaction open** |
| **Record** | same task | only if `attempt_count` still matches the claim (fencing): `sent`, retry with backoff (30s → 15 min, jitter), or `failed` after `NOTIFICATION_MAX_ATTEMPTS` |

Emission points: run started (executor claim), task retrying (executor + reaper), run
succeeded/failed (orchestrator), run timed out (scheduler).

**Why an outbox rather than sending inline?** Sending inside the engine transaction would hold row
locks during a network call. Sending after commit risks "run failed, nobody was told" if the
process dies in between. The outbox makes the alert part of the same atomic write as the failure.

**Why a lease instead of a reaper?** If a worker dies mid-send, the lease simply expires and the
next tick claims the row again. The fencing check then stops the original (late) message from
recording or sending twice. The guarantee is **at-least-once**: a duplicate alert is acceptable,
a lost one is not.

### Alternatives considered

| Option | Why not |
|---|---|
| Send from the orchestrator directly | Network I/O inside a locked transaction, and lost on crash |
| Postgres `LISTEN/NOTIFY` | Not durable: a listener that's down misses events |
| Redis stream as the outbox | A second source of truth (contradicts ADR-0001) |
| Deliver inline in the scheduler | A slow webhook would delay cron firing for every workflow |
| Vendor SDKs (`openai`, `anthropic`) | Two HTTP stacks, two retry policies, harder to fake. Revisit if streaming or batch APIs are needed |

## Testing strategy

- **Adapters**: `httpx.MockTransport` asserts the exact URL, headers and body we send, and how
  every response class is handled (200, 401, 429, 5xx/529, timeouts, refusals). No network.
- **Operators**: scripted providers prove credential resolution, failover only on retryable
  errors, and cost metering.
- **Real Postgres**: encryption at rest via raw SQL, owner isolation, delivery retry/backoff/give-up,
  lease expiry with a lost worker plus a late duplicate, signed webhook payloads, spec→subscription
  sync, and the usage report.
- **Live**: API + Celery worker + scheduler processes: connection test, channel test, an event
  that triggers the lead workflow, and alerts delivered through the outbox to `sent`.

## Going live checklist

1. Create a bot with @BotFather, then store its token: `POST /connections` (type `telegram`).
2. Message the bot, then `GET /connections/{id}/telegram-chats` to find your `chat_id`.
3. `POST /channels` and then `POST /channels/{id}:test`.
4. Store your OpenAI/Anthropic keys as connections (or platform env vars) and run `:test` on each.
5. Set `NUROFLOW_LLM_PRICING`, set provider spending limits in each provider console, and
   switch both modes to `live`.
