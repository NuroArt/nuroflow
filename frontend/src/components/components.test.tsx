import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Overview } from "../lib/types";
import { RunsChart } from "./RunsChart";
import { StatusBadge } from "./StatusBadge";

class ResizeObserverStub {
  observe() {}
  disconnect() {}
}
globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;

describe("StatusBadge", () => {
  it("always carries a text label, even in compact mode", () => {
    render(<><StatusBadge status="failed" /><StatusBadge status="up_for_retry" compact /></>);
    expect(screen.getByText("Failed")).toBeVisible();
    expect(screen.getByText("Retrying")).toHaveClass("sr-only"); // available to screen readers
  });
});

const overview: Overview = {
  since: "2026-11-05T00:00:00Z", bucket: "hour", runs: 5,
  by_status: { succeeded: 3, failed: 2 }, success_rate: 0.6, duration_p50_seconds: 2,
  duration_p95_seconds: 9, active_runs: 0, queued_tasks: 0, failing_workflows: [],
  series: [
    { at: "2026-11-05T10:00:00Z", succeeded: 3, failed: 1, other: 0 },
    { at: "2026-11-05T11:00:00Z", succeeded: 0, failed: 1, other: 0 },
  ],
};

describe("RunsChart", () => {
  it("has a legend, a labelled figure and a table view", () => {
    render(<RunsChart data={overview} />);
    expect(screen.getByRole("img", { name: /runs per hour/i })).toBeInTheDocument();
    expect(screen.getByLabelText("Legend")).toHaveTextContent(/Succeeded\s*Failed\s*Other/);
    fireEvent.click(screen.getByText("Show table"));
    const rows = screen.getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(rows[1]).toHaveTextContent(/3\s*1\s*0/);
  });
});

import { Timeline } from "./Timeline";
import type { RunDetail } from "../lib/types";

describe("Timeline", () => {
  it("keeps every bar inside the track and lists waiting tasks last", () => {
    const base = { operator: "noop", queue: "default", attempt_count: 1, max_attempts: 1, next_attempt_at: null, error_message: null };
    const run: RunDetail = {
      id: "r", workflow_id: "w", status: "running", trigger_type: "manual", admitted_at: null, error_message: null,
      created_at: "2026-11-05T10:00:00.000Z", started_at: "2026-11-05T10:00:01.000Z", finished_at: null,
      tasks: [
        { ...base, id: "2", task_key: "later", status: "pending", queued_at: null, started_at: null, finished_at: null },
        // Queued before the run counted as started: the old origin made this negative.
        { ...base, id: "1", task_key: "first", status: "running", queued_at: "2026-11-05T10:00:00.200Z", started_at: "2026-11-05T10:00:01.000Z", finished_at: null },
      ],
    };
    const { container } = render(<Timeline run={run} now={Date.parse("2026-11-05T10:00:05Z")} />);
    const keys = [...container.querySelectorAll("li .font-mono")].map((n) => n.textContent);
    expect(keys).toEqual(["first", "later"]);
    for (const bar of container.querySelectorAll<HTMLElement>("span.absolute")) {
      expect(parseFloat(bar.style.left)).toBeGreaterThanOrEqual(0);
      expect(parseFloat(bar.style.left) + parseFloat(bar.style.width)).toBeLessThanOrEqual(100.0001);
    }
  });
});
