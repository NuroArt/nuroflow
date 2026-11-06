import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pause, Play } from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { DagView } from "../components/DagView";
import { PageHeader } from "../components/Layout";
import { StatusBadge } from "../components/StatusBadge";
import { Empty, ErrorNotice, Spinner } from "../components/ui";
import { api } from "../lib/api";
import { durationBetween, formatDuration, relativeTime, shortId } from "../lib/format";
import type { Page, Run, Trigger, Workflow, WorkflowDetail } from "../lib/types";

export function WorkflowsPage() {
  const workflows = useQuery({ queryKey: ["workflows"], queryFn: () => api<Page<Workflow>>("/workflows?limit=100") });
  return (
    <>
      <PageHeader title="Workflows" subtitle="Versioned DAG definitions. Create them with the API or by importing YAML." />
      {workflows.isError && <ErrorNotice error={workflows.error} />}
      {workflows.isPending ? <Spinner /> : workflows.data?.items.length ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {workflows.data.items.map((w) => (
            <Link key={w.id} to={`/workflows/${w.id}`} className="card group p-4 transition hover:border-accent/50 hover:shadow-glow">
              <div className="flex items-start justify-between gap-2">
                <h2 className="font-medium group-hover:text-accent-strong">{w.name}</h2>
                {w.is_paused && <span className="rounded-full border border-warn/40 px-2 py-0.5 text-xs text-warn">Paused</span>}
              </div>
              <p className="mt-0.5 font-mono text-xs text-ink-faint">{w.slug}</p>
              {w.description && <p className="mt-2 line-clamp-2 text-sm text-ink-muted">{w.description}</p>}
              <div className="mt-3 flex flex-wrap gap-1.5">
                {w.tags.map((t) => <span key={t} className="rounded bg-surface-3 px-1.5 py-0.5 text-[11px] text-ink-muted">{t}</span>)}
              </div>
            </Link>
          ))}
        </div>
      ) : (
        <Empty title="No workflows yet">
          Import the reference workflow: <code className="font-mono text-ink">POST /api/v1/workflows:import</code> with{" "}
          <code className="font-mono text-ink">examples/workflows/lead-qualification.yaml</code>.
        </Empty>
      )}
    </>
  );
}

export function WorkflowDetailPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const workflow = useQuery({ queryKey: ["workflow", id], queryFn: () => api<WorkflowDetail>(`/workflows/${id}`) });
  const triggers = useQuery({ queryKey: ["triggers", id], queryFn: () => api<Trigger[]>(`/workflows/${id}/triggers`) });
  const runs = useQuery({
    queryKey: ["runs", "workflow", id],
    queryFn: () => api<Page<Run>>(`/runs?workflow_id=${id}&limit=15`),
    refetchInterval: 5_000,
  });
  const runNow = useMutation({
    mutationFn: () => api<Run>(`/workflows/${id}/runs`, { method: "POST", json: {} }),
    onSuccess: (run) => navigate(`/runs/${run.id}`),
  });
  const pause = useMutation({
    mutationFn: (paused: boolean) => api<Workflow>(`/workflows/${id}:pause`, { method: "POST", json: { paused } }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["workflow", id] }),
  });

  if (workflow.isPending) return <Spinner />;
  if (workflow.isError) return <ErrorNotice error={workflow.error} />;
  const w = workflow.data;
  const tasks = w.current_version?.definition.tasks ?? [];

  return (
    <>
      <PageHeader
        title={w.name}
        subtitle={<span className="font-mono">{w.slug} · v{w.current_version?.version ?? "-"} · {tasks.length} tasks</span>}
        actions={<>
          <button className="btn-ghost" onClick={() => pause.mutate(!w.is_paused)} disabled={pause.isPending}>
            {w.is_paused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />} {w.is_paused ? "Resume" : "Pause"}
          </button>
          <button className="btn-primary" onClick={() => runNow.mutate()} disabled={runNow.isPending || w.is_paused}>
            <Play className="h-4 w-4" /> Run now
          </button>
        </>}
      />
      {runNow.isError && <div className="mb-4"><ErrorNotice error={runNow.error} /></div>}
      <div className="space-y-6">
        <DagView tasks={tasks} />
        <div className="grid gap-6 lg:grid-cols-[2fr_1fr]">
          <section className="card overflow-hidden">
            <h2 className="border-b border-line px-4 py-3 font-medium">Runs</h2>
            {runs.data?.items.length ? <RunTable runs={runs.data.items} /> : <p className="p-4 text-sm text-ink-muted">No runs yet.</p>}
          </section>
          <section className="card">
            <h2 className="border-b border-line px-4 py-3 font-medium">Triggers</h2>
            {triggers.data?.length ? (
              <ul>
                {triggers.data.map((t) => (
                  <li key={t.id} className="border-t border-line px-4 py-3 text-sm first:border-0">
                    <div className="flex items-center justify-between">
                      <span className="rounded bg-surface-3 px-1.5 py-0.5 text-xs uppercase text-ink-muted">{t.type}</span>
                      <span className="text-xs text-ink-faint">{t.is_enabled ? "enabled" : "disabled"}</span>
                    </div>
                    <p className="mt-1.5 truncate font-mono text-xs">
                      {t.cron_expression ?? t.event_type ?? t.webhook_url ?? t.name}
                    </p>
                    {t.next_fire_at && <p className="mt-1 text-xs text-ink-faint">next {relativeTime(t.next_fire_at)} ({t.timezone})</p>}
                  </li>
                ))}
              </ul>
            ) : <p className="p-4 text-sm text-ink-muted">Manual runs only.</p>}
          </section>
        </div>
      </div>
    </>
  );
}

export function RunTable({ runs }: { runs: Run[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full">
        <thead><tr><th className="th">Run</th><th className="th">Status</th><th className="th">Trigger</th><th className="th text-right">Duration</th><th className="th text-right">Created</th></tr></thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.id} className="border-t border-line hover:bg-surface-2">
              <td className="td"><Link to={`/runs/${run.id}`} className="font-mono text-xs text-accent-strong hover:underline">{shortId(run.id)}</Link></td>
              <td className="td"><StatusBadge status={run.status} /></td>
              <td className="td text-ink-muted">{run.trigger_type}</td>
              <td className="td text-right tabular-nums text-ink-muted">{formatDuration(durationBetween(run.started_at, run.finished_at))}</td>
              <td className="td text-right text-ink-faint">{relativeTime(run.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
