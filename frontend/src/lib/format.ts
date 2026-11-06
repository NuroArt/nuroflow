export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return "-";
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

export function durationBetween(start: string | null, end: string | null, now = Date.now()): number | null {
  if (!start) return null;
  return ((end ? Date.parse(end) : now) - Date.parse(start)) / 1000;
}

export function relativeTime(iso: string | null, now = Date.now()): string {
  if (!iso) return "-";
  const diff = (now - Date.parse(iso)) / 1000;
  const future = diff < 0;
  const abs = Math.abs(diff);
  const text =
    abs < 45 ? "just now"
    : abs < 3600 ? `${Math.round(abs / 60)} min`
    : abs < 86_400 ? `${Math.round(abs / 3600)} h`
    : `${Math.round(abs / 86_400)} d`;
  if (text === "just now") return text;
  return future ? `in ${text}` : `${text} ago`;
}

export function percent(ratio: number | null | undefined, digits = 1): string {
  return ratio == null ? "-" : `${(ratio * 100).toFixed(digits)}%`;
}

export function usd(value: number): string {
  return value < 0.01 && value > 0 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
}

export function compact(n: number): string {
  return new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

export function shortId(id: string): string {
  return id.slice(-8);
}
