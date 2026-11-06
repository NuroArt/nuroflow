import { useQuery } from "@tanstack/react-query";
import { KeyRound, Send } from "lucide-react";
import { PageHeader } from "../components/Layout";
import { ErrorNotice, Spinner } from "../components/ui";
import { api } from "../lib/api";
import { compact, relativeTime, usd } from "../lib/format";
import type { Channel, Connection, Delivery, LLMUsage } from "../lib/types";

const DELIVERY: Record<Delivery["status"], string> = { sent: "text-ok", pending: "text-warn", failed: "text-fail" };

export function IntegrationsPage() {
  const connections = useQuery({ queryKey: ["connections"], queryFn: () => api<Connection[]>("/connections") });
  const channels = useQuery({ queryKey: ["channels"], queryFn: () => api<Channel[]>("/channels") });
  const deliveries = useQuery({ queryKey: ["deliveries"], queryFn: () => api<Delivery[]>("/notification-deliveries?limit=20"), refetchInterval: 10_000 });
  const usage = useQuery({ queryKey: ["usage", 30], queryFn: () => api<LLMUsage>("/usage/llm?days=30") });

  return (
    <>
      <PageHeader title="Integrations" subtitle="Credentials, alert channels and AI spend. Secrets are write-only and never shown." />
      {[connections, channels].map((q, i) => q.isError && <ErrorNotice key={i} error={q.error} />)}
      <div className="grid gap-6 lg:grid-cols-2">
        <section className="card">
          <h2 className="flex items-center gap-2 border-b border-line px-4 py-3 font-medium"><KeyRound className="h-4 w-4 text-accent" /> Connections</h2>
          {connections.isPending ? <Spinner /> : connections.data?.length ? (
            <ul>{connections.data.map((c) => (
              <li key={c.id} className="flex items-center justify-between border-t border-line px-4 py-3 first:border-0">
                <div><p className="font-mono text-sm">{c.name}</p>{c.description && <p className="text-xs text-ink-faint">{c.description}</p>}</div>
                <span className="rounded bg-surface-3 px-1.5 py-0.5 text-xs text-ink-muted">{c.type}</span>
              </li>))}
            </ul>
          ) : <p className="p-4 text-sm text-ink-muted">No connections. Add one with <code className="font-mono">POST /api/v1/connections</code>.</p>}
        </section>
        <section className="card">
          <h2 className="flex items-center gap-2 border-b border-line px-4 py-3 font-medium"><Send className="h-4 w-4 text-accent" /> Alert channels</h2>
          {channels.isPending ? <Spinner /> : channels.data?.length ? (
            <ul>{channels.data.map((c) => (
              <li key={c.id} className="flex items-center justify-between border-t border-line px-4 py-3 first:border-0">
                <div><p className="font-mono text-sm">{c.name}</p><p className="truncate text-xs text-ink-faint">{c.target}</p></div>
                <span className="text-xs text-ink-muted">{c.type}{!c.is_enabled && " · disabled"}</span>
              </li>))}
            </ul>
          ) : <p className="p-4 text-sm text-ink-muted">No channels yet.</p>}
        </section>
      </div>

      <section className="card mt-6 overflow-hidden">
        <h2 className="border-b border-line px-4 py-3 font-medium">LLM usage (30 days)</h2>
        {usage.data?.models.length ? (
          <table className="w-full">
            <thead><tr><th className="th">Model</th><th className="th text-right">Calls</th><th className="th text-right">Tokens in → out</th><th className="th text-right">Cost</th></tr></thead>
            <tbody>{usage.data.models.map((m) => (
              <tr key={`${m.provider}/${m.model}`} className="border-t border-line">
                <td className="td font-mono text-xs">{m.provider}/{m.model}</td>
                <td className="td text-right tabular-nums">{m.calls}</td>
                <td className="td text-right tabular-nums text-ink-muted">{compact(m.input_tokens)} → {compact(m.output_tokens)}</td>
                <td className="td text-right tabular-nums">{usd(m.cost_usd)}</td>
              </tr>))}
            </tbody>
          </table>
        ) : <p className="p-4 text-sm text-ink-muted">No AI calls recorded.</p>}
      </section>

      <section className="card mt-6 overflow-hidden">
        <h2 className="border-b border-line px-4 py-3 font-medium">Recent alert deliveries</h2>
        {deliveries.data?.length ? (
          <table className="w-full">
            <thead><tr><th className="th">Event</th><th className="th">Status</th><th className="th text-right">Attempts</th><th className="th">Last error</th><th className="th text-right">Queued</th></tr></thead>
            <tbody>{deliveries.data.map((d) => (
              <tr key={d.id} className="border-t border-line">
                <td className="td font-mono text-xs">{d.event}</td>
                <td className={`td text-xs font-medium uppercase ${DELIVERY[d.status]}`}>{d.status}</td>
                <td className="td text-right tabular-nums">{d.attempt_count}</td>
                <td className="td max-w-xs truncate text-xs text-ink-faint">{d.last_error ?? "-"}</td>
                <td className="td text-right text-ink-faint">{relativeTime(d.created_at)}</td>
              </tr>))}
            </tbody>
          </table>
        ) : <p className="p-4 text-sm text-ink-muted">No alerts sent yet.</p>}
      </section>
    </>
  );
}
