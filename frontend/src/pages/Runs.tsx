import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ban } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { DagView } from "../components/DagView";
import { PageHeader } from "../components/Layout";
import { LogViewer } from "../components/LogViewer";
import { StatusBadge } from "../components/StatusBadge";
import { Timeline } from "../components/Timeline";
import { Empty, ErrorNotice, LiveDot, Spinner } from "../components/ui";
import { useLogStream, useNow, useRunStream } from "../hooks/streams";
import { api } from "../lib/api";
import { durationBetween, formatDuration, relativeTime, shortId, usd } from "../lib/format";
import type { Attempt, Page, Run, RunDetail, RunStatus, WorkflowDetail } from "../lib/types";
import { RunTable } from "./Workflows";

const FILTERS: (RunStatus | "all")[] = ["all", "running", "succeeded", "failed", "pending", "cancelled"];

export function RunsPage() {
  const [status, setStatus] = useState<RunStatus | "all">("all");
  const runs = useInfiniteQuery({
    queryKey: ["runs", "list", status],
    queryFn: ({ pageParam }) =>
      api<Page<Run>>(`/runs?limit=25${status === "all" ? "" : `&status=${status}`}${pageParam ? `&cursor=${pageParam}` : ""}`),
    initialPageParam: "",
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: 5_000,
  });
  const items = runs.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <>
      <PageHeader title="Runs" subtitle="Every execution, newest first." actions={
        <div className="flex flex-wrap rounded-lg border border-line p-0.5" role="group" aria-label="Status filter">
          {FILTERS.map((f) => (
            <button key={f} aria-pressed={status === f} onClick={() => setStatus(f)}
              className={`rounded-md px-2.5 py-1 text-xs capitalize ${status === f ? "bg-accent/15 text-ink" : "text-ink-muted hover:text-ink"}`}>{f}</button>
          ))}
        </div>
      } />
      {runs.isError && <ErrorNotice error={runs.error} />}
      {runs.isPending ? <Spinner /> : items.length ? (
        <section className="card overflow-hidden">
          <RunTable runs={items} />
          {runs.hasNextPage && (
            <div className="border-t border-line p-3 text-center">
              <button className="btn-ghost" onClick={() => void runs.fetchNextPage()} disabled={runs.isFetchingNextPage}>Load more</button>
            </div>
          )}
        </section>
      ) : <Empty title="No runs match this filter" />}
    </>
  );
}

export function RunDetailPage() {
  const { id = "" } = useParams();
  const client = useQueryClient();
  const initial = useQuery({ queryKey: ["run", id], queryFn: () => api<RunDetail>(`/runs/${id}`) });
  const { run, state } = useRunStream(id, initial.data);
  const workflow = useQuery({
    queryKey: ["workflow", run?.workflow_id],
    queryFn: () => api<WorkflowDetail>(`/workflows/${run!.workflow_id}`),
    enabled: Boolean(run?.workflow_id),
  });
  const [selected, setSelected] = useState<string | null>(null);
  const live = run ? run.status === "pending" || run.status === "running" : false;
  const now = useNow(live);
  const cancel = useMutation({
    mutationFn: () => api<Run>(`/runs/${id}:cancel`, { method: "POST" }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["run", id] }),
  });

  if (initial.isError) return <ErrorNotice error={initial.error} />;
  if (!run) return <Spinner />;
  const selectedKey = selected ?? run.tasks.find((t) => t.status === "failed")?.task_key ?? run.tasks[0]?.task_key ?? null;
  const task = run.tasks.find((t) => t.task_key === selectedKey) ?? null;
  // Prefer the definition this run executed; fall back to task runs when it isn't loaded yet.
  const specs = workflow.data?.current_version?.definition.tasks ?? run.tasks.map((t) => ({ key: t.task_key, operator: t.operator }));

  return (
    <>
      <PageHeader
        title={<span className="flex items-center gap-3">Run <span className="font-mono text-lg text-ink-muted">{shortId(run.id)}</span><StatusBadge status={run.status} /></span>}
        subtitle={<span className="flex flex-wrap items-center gap-x-3 gap-y-1">
          {workflow.data && <Link to={`/workflows/${run.workflow_id}`} className="text-accent-strong hover:underline">{workflow.data.name}</Link>}
          <span>{run.trigger_type} · created {relativeTime(run.created_at, now)}</span>
          <span className="tabular-nums">duration {formatDuration(durationBetween(run.started_at, run.finished_at, now))}</span>
          <LiveDot state={state} />
        </span>}
        actions={live && <button className="btn-ghost" onClick={() => cancel.mutate()} disabled={cancel.isPending}><Ban className="h-4 w-4" /> Cancel run</button>}
      />
      {run.error_message && <div className="mb-4 rounded-lg border border-fail/40 bg-fail/10 px-4 py-2 text-sm">{run.error_message}</div>}
      {run.admitted_at === null && run.status === "pending" && (
        <div className="mb-4 rounded-lg border border-warn/40 bg-warn/10 px-4 py-2 text-sm">Waiting for a free slot (max_active_runs reached).</div>
      )}
      <div className="space-y-6">
        <DagView tasks={specs} runs={run.tasks} selected={selectedKey} onSelect={setSelected} />
        <div className="grid gap-6 xl:grid-cols-2">
          <Timeline run={run} now={now} onSelect={setSelected} />
          {task ? <TaskPanel key={task.id} taskId={task.id} title={task.task_key} status={task.status} attempts={task.attempt_count} maxAttempts={task.max_attempts} /> : <Empty title="No tasks" />}
        </div>
      </div>
    </>
  );
}

function TaskPanel({ taskId, title, status, attempts, maxAttempts }: { taskId: string; title: string; status: RunDetail["tasks"][number]["status"]; attempts: number; maxAttempts: number }) {
  const { lines, state } = useLogStream(taskId);
  const history = useQuery({
    queryKey: ["attempts", taskId, status, attempts],
    queryFn: () => api<Attempt[]>(`/task-runs/${taskId}/attempts`),
  });
  return (
    <section className="card p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="font-mono font-medium">{title}</h2>
        <span className="flex items-center gap-2 text-xs text-ink-muted">attempt {attempts}/{maxAttempts} <StatusBadge status={status} /></span>
      </div>
      <LogViewer lines={lines} state={state} />
      <h3 className="label mt-4">Attempts</h3>
      <ul className="mt-2 space-y-2">
        {history.data?.map((a) => (
          <li key={a.id} className="rounded-lg border border-line bg-surface-2 px-3 py-2 text-xs">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span>#{a.attempt_number} · <span className="uppercase">{a.status}</span>{a.worker_id && <span className="text-ink-faint"> · {a.worker_id}</span>}</span>
              <span className="tabular-nums text-ink-muted">{a.duration_ms != null ? formatDuration(a.duration_ms / 1000) : "running"}</span>
            </div>
            {a.metrics.llm && (
              <p className="mt-1 text-ink-muted">
                {a.metrics.llm.provider}/{a.metrics.llm.model} · {a.metrics.llm.input_tokens}→{a.metrics.llm.output_tokens} tokens
                {a.metrics.llm.cost_usd != null && ` · ${usd(a.metrics.llm.cost_usd)}`}
              </p>
            )}
            {a.error_message && <p className="mt-1 text-fail"><span className="font-mono">{a.error_type}</span>: {a.error_message}</p>}
            {a.traceback && (
              <details className="mt-1"><summary className="cursor-pointer text-ink-faint">traceback</summary>
                <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap text-[11px] text-ink-muted">{a.traceback}</pre></details>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
