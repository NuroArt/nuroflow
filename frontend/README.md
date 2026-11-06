# NuroFlow dashboard

React 18 + TypeScript + Tailwind + TanStack Query + React Flow. Dark, high-contrast UI for
watching workflows run.

| Page | What it shows |
|---|---|
| **Overview** | Runs, success rate, p50/p95 duration, active runs, LLM spend; throughput chart; failing workflows; recent runs |
| **Workflows** | Cards per workflow → DAG, triggers, run history, *Run now* / *Pause* |
| **Run** | Live DAG coloured by task status, Gantt timeline (queue wait vs execution), per-task live log tail, attempts with errors, tracebacks and token/cost |
| **Integrations** | Connections (secrets never shown), alert channels, LLM usage per model, delivery history |

```bash
npm ci          # or: make web-install
npm run dev     # http://localhost:5173 (proxies /api → http://127.0.0.1:8000)
npm test        # vitest: SSE parser, DAG layout, token refresh, a11y of chart/badges
npm run build   # → dist/ (served by nginx in Phase 7)
```

Design notes (details in [docs/OBSERVABILITY.md](../docs/OBSERVABILITY.md#dashboard)):

- **Live data via SSE over `fetch()`**, not `EventSource`: an Authorization header instead of a
  JWT in the URL, abortable, resumes with `Last-Event-ID`.
- **Access token in memory, refresh token in sessionStorage**, with single-flight refresh
  (the API revokes the session if a rotated refresh token is reused).
- **Colour is never the only signal.** Every status has an icon and a label. The chart pair
  (`#3d8bff` / `#e5484d`) was validated for colour-vision deficiency, and the chart has a
  legend, a tooltip and a table view.
