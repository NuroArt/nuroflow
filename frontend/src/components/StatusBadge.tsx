import { Ban, CheckCircle2, Clock, Loader2, MinusCircle, RotateCw, XCircle } from "lucide-react";
import type { RunStatus, TaskStatus } from "../lib/types";

type Status = RunStatus | TaskStatus;

// Colour is never the only signal: every status has its own icon and a text label.
const STYLES: Record<Status, { label: string; className: string; Icon: typeof CheckCircle2; spin?: boolean }> = {
  succeeded: { label: "Succeeded", className: "text-ok bg-ok/10 border-ok/30", Icon: CheckCircle2 },
  failed: { label: "Failed", className: "text-fail bg-fail/10 border-fail/30", Icon: XCircle },
  running: { label: "Running", className: "text-accent-strong bg-accent/10 border-accent/40", Icon: Loader2, spin: true },
  up_for_retry: { label: "Retrying", className: "text-warn bg-warn/10 border-warn/30", Icon: RotateCw },
  queued: { label: "Queued", className: "text-ink-muted bg-surface-3 border-line", Icon: Clock },
  pending: { label: "Pending", className: "text-ink-muted bg-surface-3 border-line", Icon: Clock },
  skipped: { label: "Skipped", className: "text-ink-faint bg-surface-2 border-line", Icon: MinusCircle },
  cancelled: { label: "Cancelled", className: "text-ink-faint bg-surface-2 border-line", Icon: Ban },
};

export function StatusBadge({ status, compact = false }: { status: Status; compact?: boolean }) {
  const style = STYLES[status] ?? STYLES.pending;
  const { Icon } = style;
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium ${style.className}`}
      title={style.label}
    >
      <Icon aria-hidden className={`h-3.5 w-3.5 ${style.spin ? "animate-spin" : ""}`} />
      {compact ? <span className="sr-only">{style.label}</span> : style.label}
    </span>
  );
}

export function statusStroke(status: Status): string {
  return status === "succeeded" ? "#3d8bff"
    : status === "failed" ? "#e5484d"
    : status === "running" ? "#6aa6ff"
    : status === "up_for_retry" ? "#fab219"
    : "#2b3850";
}
