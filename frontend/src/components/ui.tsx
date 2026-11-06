import { AlertTriangle } from "lucide-react";
import type { ReactNode } from "react";
import { ApiError } from "../lib/api";

export function StatCard({ label, value, hint, accent = false }: { label: string; value: ReactNode; hint?: ReactNode; accent?: boolean }) {
  return (
    <div className={`card p-4 ${accent ? "shadow-glow" : ""}`}>
      <p className="label">{label}</p>
      <p className="mt-2 text-2xl font-semibold tabular-nums tracking-tight">{value}</p>
      {hint && <p className="mt-1 text-xs text-ink-faint">{hint}</p>}
    </div>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 p-6 text-sm text-ink-muted" role="status">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-accent border-t-transparent" />
      {label}…
    </div>
  );
}

export function ErrorNotice({ error }: { error: unknown }) {
  const message = error instanceof ApiError ? error.message : "Something went wrong.";
  const requestId = error instanceof ApiError ? error.requestId : undefined;
  return (
    <div className="card flex items-start gap-3 border-fail/40 p-4 text-sm" role="alert">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-fail" aria-hidden />
      <div>
        <p>{message}</p>
        {requestId && <p className="mt-1 font-mono text-xs text-ink-faint">request {requestId}</p>}
      </div>
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="card border-dashed p-10 text-center">
      <p className="font-medium">{title}</p>
      {children && <div className="mt-2 text-sm text-ink-muted">{children}</div>}
    </div>
  );
}

export function LiveDot({ state }: { state: string }) {
  const live = state === "live";
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-ink-muted">
      <span className={`h-2 w-2 rounded-full ${live ? "animate-pulse bg-accent" : state === "closed" ? "bg-neutral" : "bg-warn"}`} />
      {live ? "Live" : state === "closed" ? "Final" : state === "reconnecting" ? "Reconnecting" : "Connecting"}
    </span>
  );
}
