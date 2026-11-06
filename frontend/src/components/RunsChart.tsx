import { useEffect, useMemo, useRef, useState } from "react";
import type { Overview } from "../lib/types";

// Validated pair on the #0b0f17 surface: CVD ΔE 26.4 (protan), normal-vision ΔE 33.9.
// "Other" (pending/running/cancelled) is a neutral, not an identity hue.
const SERIES = [
  { key: "succeeded", label: "Succeeded", color: "#3d8bff" },
  { key: "failed", label: "Failed", color: "#e5484d" },
  { key: "other", label: "Other", color: "#5b6474" },
] as const;

const H = 220;
const PAD = { top: 12, right: 8, bottom: 26, left: 36 };
const GAP = 2; // surface gap between stacked segments

function niceMax(value: number): number {
  if (value <= 4) return 4;
  const step = 10 ** Math.floor(Math.log10(value));
  return Math.ceil(value / step) * step;
}

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(640);
  useEffect(() => {
    if (!ref.current) return;
    const observer = new ResizeObserver(([entry]) => entry && setWidth(entry.contentRect.width));
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

export function RunsChart({ data }: { data: Overview }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const [asTable, setAsTable] = useState(false);
  const points = data.series;
  const fmt = (iso: string) =>
    new Date(iso).toLocaleString(undefined, data.bucket === "hour" ? { hour: "2-digit", minute: "2-digit" } : { month: "short", day: "numeric" });

  const { max, slot, bar } = useMemo(() => {
    const totals = points.map((p) => p.succeeded + p.failed + p.other);
    const max = niceMax(Math.max(0, ...totals));
    const slot = (width - PAD.left - PAD.right) / Math.max(points.length, 1);
    return { max, slot, bar: Math.max(2, Math.min(28, slot * 0.62)) };
  }, [points, width]);
  const y = (v: number) => PAD.top + (H - PAD.top - PAD.bottom) * (1 - v / max);
  const ticks = [0, max / 2, max];
  const labelEvery = Math.ceil(points.length / Math.max(2, Math.floor(width / 70)));

  return (
    <div className="card p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 className="font-medium">Run throughput</h2>
        <div className="flex items-center gap-4">
          <ul className="flex gap-3 text-xs text-ink-muted" aria-label="Legend">
            {SERIES.map((s) => (
              <li key={s.key} className="flex items-center gap-1.5">
                <span className="h-2.5 w-2.5 rounded-sm" style={{ background: s.color }} /> {s.label}
              </li>
            ))}
          </ul>
          <button className="text-xs text-ink-faint underline-offset-2 hover:text-ink hover:underline" onClick={() => setAsTable((v) => !v)}>
            {asTable ? "Show chart" : "Show table"}
          </button>
        </div>
      </div>

      {asTable ? (
        <div className="max-h-64 overflow-auto">
          <table className="w-full text-sm">
            <thead><tr><th className="th">Time</th>{SERIES.map((s) => <th key={s.key} className="th text-right">{s.label}</th>)}</tr></thead>
            <tbody>
              {points.map((p) => (
                <tr key={p.at} className="border-t border-line">
                  <td className="td text-ink-muted">{fmt(p.at)}</td>
                  {SERIES.map((s) => <td key={s.key} className="td text-right tabular-nums">{p[s.key]}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div ref={ref} className="relative" onMouseLeave={() => setHover(null)}>
          <svg width={width} height={H} role="img" aria-label={`Runs per ${data.bucket}, stacked by outcome`}>
            {ticks.map((t) => (
              <g key={t}>
                <line x1={PAD.left} x2={width - PAD.right} y1={y(t)} y2={y(t)} stroke="#1a2233" strokeDasharray={t ? "2 4" : undefined} />
                <text x={PAD.left - 8} y={y(t)} dy="0.32em" textAnchor="end" className="fill-ink-faint text-[10px] tabular-nums">{t}</text>
              </g>
            ))}
            {points.map((p, i) => {
              const cx = PAD.left + slot * i + slot / 2;
              let base = 0;
              const parts = SERIES.filter((s) => p[s.key] > 0);
              return (
                <g key={p.at}>
                  {/* Hit target spans the whole slot, larger than the bar. */}
                  <rect x={cx - slot / 2} y={PAD.top} width={slot} height={H - PAD.top - PAD.bottom}
                    fill={hover === i ? "#ffffff08" : "transparent"} onMouseEnter={() => setHover(i)} />
                  {parts.map((s, j) => {
                    const top = y(base + p[s.key]);
                    const bottom = y(base) - (j > 0 ? GAP : 0);
                    base += p[s.key];
                    const h = Math.max(1, bottom - top);
                    const isTop = j === parts.length - 1;
                    return (
                      <path key={s.key} pointerEvents="none" fill={s.color}
                        d={roundedTop(cx - bar / 2, top, bar, h, isTop ? Math.min(4, bar / 2, h) : 0)} />
                    );
                  })}
                  {i % labelEvery === 0 && (
                    <text x={cx} y={H - 8} textAnchor="middle" className="fill-ink-faint text-[10px]">{fmt(p.at)}</text>
                  )}
                </g>
              );
            })}
          </svg>
          {hover !== null && points[hover] && (
            <Tooltip x={PAD.left + slot * hover + slot / 2} width={width} title={fmt(points[hover].at)}
              rows={SERIES.map((s) => ({ ...s, value: points[hover]![s.key] }))} />
          )}
        </div>
      )}
    </div>
  );
}

function roundedTop(x: number, y: number, w: number, h: number, r: number): string {
  if (r <= 0) return `M${x},${y}h${w}v${h}h${-w}Z`;
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

function Tooltip({ x, width, title, rows }: { x: number; width: number; title: string; rows: { label: string; color: string; value: number }[] }) {
  const left = Math.min(Math.max(x - 80, 0), width - 160);
  const total = rows.reduce((sum, r) => sum + r.value, 0);
  return (
    <div className="pointer-events-none absolute top-2 w-40 rounded-lg border border-line bg-surface-2/95 p-2.5 text-xs shadow-xl backdrop-blur" style={{ left }}>
      <p className="mb-1.5 font-medium">{title}</p>
      {rows.map((r) => (
        <p key={r.label} className="flex items-center justify-between gap-2 text-ink-muted">
          <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-sm" style={{ background: r.color }} />{r.label}</span>
          <span className="tabular-nums text-ink">{r.value}</span>
        </p>
      ))}
      <p className="mt-1 flex justify-between border-t border-line pt-1 text-ink-muted">Total <span className="tabular-nums text-ink">{total}</span></p>
    </div>
  );
}
