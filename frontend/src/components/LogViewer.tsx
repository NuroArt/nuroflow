import { useEffect, useRef, useState } from "react";
import type { LogLine } from "../lib/types";
import { LiveDot } from "./ui";

const LEVEL: Record<LogLine["level"], string> = {
  debug: "text-ink-faint",
  info: "text-accent-strong",
  warning: "text-warn",
  error: "text-fail",
};

export function LogViewer({ lines, state }: { lines: LogLine[]; state: string }) {
  const box = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  useEffect(() => {
    if (follow && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines, follow]);

  return (
    <div className="overflow-hidden rounded-lg border border-line bg-surface-0">
      <div className="flex items-center justify-between border-b border-line px-3 py-1.5">
        <LiveDot state={state} />
        <label className="flex items-center gap-1.5 text-xs text-ink-faint">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} className="accent-accent" /> Follow
        </label>
      </div>
      <div ref={box} className="max-h-72 overflow-auto p-3 font-mono text-xs leading-relaxed" aria-live="polite">
        {lines.length === 0 && <p className="text-ink-faint">{state === "closed" ? "This attempt wrote no log lines." : "Waiting for log lines…"}</p>}
        {lines.map((l) => (
          <div key={l.id} className="flex gap-3 whitespace-pre-wrap break-words">
            <span className="shrink-0 tabular-nums text-ink-faint">{new Date(l.created_at).toLocaleTimeString()}</span>
            <span className={`w-14 shrink-0 uppercase ${LEVEL[l.level]}`}>{l.level}</span>
            <span className="shrink-0 text-ink-faint">#{l.attempt_number}</span>
            <span className="min-w-0">
              {l.message}
              {Object.keys(l.context).length > 0 && (
                <span className="text-ink-faint"> {Object.entries(l.context).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`).join(" ")}</span>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
