import { useEffect, useState } from "react";
import { followStream } from "../lib/sse";
import type { LogLine, RunDetail } from "../lib/types";

type StreamState = "connecting" | "live" | "reconnecting" | "closed";

/** Live run: starts from the REST snapshot, then applies SSE snapshots until the run ends. */
export function useRunStream(runId: string, initial: RunDetail | undefined) {
  const [run, setRun] = useState<RunDetail | undefined>(initial);
  const [state, setState] = useState<StreamState>("connecting");
  useEffect(() => setRun(initial), [initial]);
  useEffect(() => {
    const controller = new AbortController();
    followStream(`/runs/${runId}/stream`, {
      signal: controller.signal,
      onStateChange: setState,
      onMessage: (m) => {
        if (m.event === "snapshot") {
          const snapshot = JSON.parse(m.data) as RunDetail;
          setRun((previous) => ({ ...previous, ...snapshot }));
        }
      },
    }).catch(() => setState("closed"));
    return () => controller.abort();
  }, [runId]);
  return { run, state };
}

/** Live log tail for one task run. Resets when the task changes. */
export function useLogStream(taskRunId: string | null) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [state, setState] = useState<StreamState>("connecting");
  useEffect(() => {
    setLines([]);
    if (!taskRunId) return;
    const controller = new AbortController();
    followStream(`/task-runs/${taskRunId}/logs/stream`, {
      signal: controller.signal,
      onStateChange: setState,
      onMessage: (m) => {
        if (m.event === "log") {
          const line = JSON.parse(m.data) as LogLine;
          // Reconnects resume via Last-Event-ID, but de-duplicate defensively anyway.
          setLines((prev) => (prev.some((l) => l.id === line.id) ? prev : [...prev, line]));
        }
      },
    }).catch(() => setState("closed"));
    return () => controller.abort();
  }, [taskRunId]);
  return { lines, state };
}

/** Re-render every second while something is running (durations tick). */
export function useNow(active: boolean): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active]);
  return now;
}
