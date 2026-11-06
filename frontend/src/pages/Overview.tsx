import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { PageHeader } from "../components/Layout";
import { RunsChart } from "../components/RunsChart";
import { StatusBadge } from "../components/StatusBadge";
import { Empty, ErrorNotice, Spinner, StatCard } from "../components/ui";
import { useState } from "react";
import { api } from "../lib/api";
import { compact, durationBetween, formatDuration, percent, relativeTime, shortId, usd } from "../lib/format";
import type { LLMUsage, Overview, Page, Run } from "../lib/types";

const RANGES = [
  { hours: 24, label: "24h" },
  { hours: 72, label: "3d" },
  { hours: 168, label: "7d" },
  { hours: 720, label: "30d" },
];

export function OverviewPage() {
  const [hours, setHours] = useState(24);
  const overview = useQuery({
    queryKey: ["overview", hours],
    queryFn: () => api<Overview>(`/stats/overview?hours=${hours}`),
    refetchInterval: 15_000,
  });
  const usage = useQuery({
    queryKey: ["usage", hours],
    queryFn: () => api<LLMUsage>(`/usage/llm?days=${Math.max(1, Math.round(hours / 24))}`),
    refetchInterval: 60_000,
  });
  const recent = useQuery({
    queryKey: ["runs", "recent"],
    queryFn: () => api<Page<Run>>("/runs?limit=8"),
    refetchInterval: 5_000,
  });

  const filters = (
    <div className="flex rounded-lg border border-line p-0.5" role="group" aria-label="Time range">
      {RANGES.map((r) => (
        <button key={r.hours} onClick={() => setHours(r.hours)} aria-pressed={hours === r.hours}
          className={`rounded-md px-3 py-1 text-xs ${hours === r.hours ? "bg-accent/15 text-ink" : "text-ink-muted hover:text-ink"}`}>
          {r.label}
        </button>
      ))}
    </div>
  );

  return (
    <>
      <PageHeader title="Overview" subtitle="Health of every workflow you own, refreshed live." actions={filters} />
      {overview.isError && <ErrorNotice error={overview.error} />}
      {overview.isPending ? <Spinner /> : overview.data && (
        <div className="space-y-6">
          <section className="grid grid-cols-2 gap-3 lg:grid-cols-6" aria-label="Key metrics">
            <StatCard label="Runs" value={compact(overview.data.runs)} hint={`last ${RANGES.find((r) => r.hours === hours)?.label}`} />
            <StatCard label="Success rate" value={percent(overview.data.success_rate)} hint="succeeded ÷ finished" accent />
            <StatCard label="p50 duration" value={formatDuration(overview.data.duration_p50_seconds)} />
            <StatCard label="p95 duration" value={formatDuration(overview.data.duration_p95_seconds)} hint="slowest 5% take longer" />
            <StatCard label="Active now" value={overview.data.active_runs} hint={`${overview.data.queued_tasks} tasks queued`} />
            <StatCard label="LLM spend" value={usage.data ? usd(usage.data.total_cost_usd) : "-"}
              hint={usage.data ? `${compact(usage.data.total_tokens)} tokens${usage.data.unpriced_calls ? ` · ${usage.data.unpriced_calls} unpriced` : ""}` : undefined} />
          </section>

          <RunsChart data={overview.data} />

          <div className="grid gap-6 lg:grid-cols-[2fr_1fr]">
            <section className="card overflow-hidden">
              <h2 className="border-b border-line px-4 py-3 font-medium">Recent runs</h2>
              {recent.data?.items.length ? (
                <table className="w-full">
                  <tbody>
                    {recent.data.items.map((run) => (
                      <tr key={run.id} className="border-t border-line first:border-0 hover:bg-surface-2">
                        <td className="td"><Link to={`/runs/${run.id}`} className="font-mono text-xs text-accent-strong hover:underline">{shortId(run.id)}</Link></td>
                        <td className="td"><StatusBadge status={run.status} /></td>
                        <td className="td text-ink-muted max-sm:hidden">{run.trigger_type}</td>
                        <td className="td text-right tabular-nums text-ink-muted">{formatDuration(durationBetween(run.started_at, run.finished_at))}</td>
                        <td className="td text-right text-ink-faint">{relativeTime(run.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : <div className="p-4"><Empty title="No runs yet">Trigger a workflow to see it here.</Empty></div>}
            </section>

            <section className="card">
              <h2 className="border-b border-line px-4 py-3 font-medium">Failing workflows</h2>
              {overview.data.failing_workflows.length ? (
                <ul>
                  {overview.data.failing_workflows.map((w) => (
                    <li key={w.workflow_id} className="border-t border-line px-4 py-3 first:border-0">
                      <Link to={`/workflows/${w.workflow_id}`} className="font-medium hover:text-accent-strong">{w.name}</Link>
                      <div className="mt-1.5 flex items-center gap-2 text-xs text-ink-muted">
                        <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-3">
                          <span className="block h-full rounded-full bg-fail" style={{ width: `${(w.failed / w.total) * 100}%` }} />
                        </span>
                        <span className="tabular-nums">{w.failed}/{w.total} failed</span>
                      </div>
                    </li>
                  ))}
                </ul>
              ) : <p className="p-4 text-sm text-ink-muted">No failures in this window.</p>}
            </section>
          </div>
        </div>
      )}
    </>
  );
}
