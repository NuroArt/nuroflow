import { durationBetween, formatDuration } from "../lib/format";
import type { RunDetail } from "../lib/types";
import { statusStroke } from "./StatusBadge";

/** Gantt view of one run: where did the time go? Queue wait is shown faintly before each bar. */
export function Timeline({ run, now, onSelect }: { run: RunDetail; now: number; onSelect?: (key: string) => void }) {
  // Tasks that have started come first, in start order; tasks still waiting go last.
  const startKey = (t: (typeof run.tasks)[number]) => {
    const iso = t.queued_at ?? t.started_at;
    return iso ? Date.parse(iso) : Number.POSITIVE_INFINITY;
  };
  const tasks = [...run.tasks].sort((a, b) => startKey(a) - startKey(b) || a.task_key.localeCompare(b.task_key));
  // Measure from creation: tasks are queued *before* the run counts as started, so
  // using started_at as the origin would put queue-wait bars at negative offsets.
  const origin = Date.parse(run.created_at);
  const end = run.finished_at ? Date.parse(run.finished_at) : now;
  const span = Math.max(end - origin, 1);
  const pct = (iso: string | null, fallback: number) =>
    Math.min(100, Math.max(0, (((iso ? Date.parse(iso) : fallback) - origin) / span) * 100));

  return (
    <div className="card p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="font-medium">Timeline</h2>
        <span className="flex items-center gap-3 text-xs text-ink-faint">
          <span className="flex items-center gap-1.5"><span className="h-2 w-3 rounded-sm bg-ink-faint/25" /> queued</span>
          <span className="tabular-nums">total {formatDuration(span / 1000)}</span>
        </span>
      </div>
      <ul className="space-y-1.5">
        {tasks.map((t) => {
          const queued = pct(t.queued_at, origin);
          const start = t.started_at ? pct(t.started_at, origin) : null;
          const finish = pct(t.finished_at, now);
          const duration = durationBetween(t.started_at, t.finished_at, now);
          return (
            <li key={t.id}>
              <button className="grid w-full grid-cols-[8rem_1fr_4.5rem] items-center gap-3 rounded-md px-1 py-0.5 text-left hover:bg-surface-2" onClick={() => onSelect?.(t.task_key)}>
                <span className="truncate font-mono text-xs">{t.task_key}</span>
                <span className="relative h-4 rounded bg-surface-2">
                  {start !== null && start > queued && (
                    <span className="absolute inset-y-1 rounded-sm bg-ink-faint/25" style={{ left: `${queued}%`, width: `${start - queued}%` }} title="queue wait" />
                  )}
                  {start !== null && (
                    <span className="absolute inset-y-0 rounded" style={{ left: `${start}%`, width: `${Math.max(finish - start, 0.6)}%`, background: statusStroke(t.status) }} />
                  )}
                </span>
                <span className="text-right text-xs tabular-nums text-ink-muted">{formatDuration(duration)}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
