// Mirrors of the API schemas the dashboard uses (see /openapi.json for the full contract).

export type RunStatus = "pending" | "running" | "succeeded" | "failed" | "cancelled";
export type TaskStatus =
  | "pending" | "queued" | "running" | "up_for_retry"
  | "succeeded" | "failed" | "skipped" | "cancelled";

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  expires_in: number;
}

export interface User { id: string; email: string; full_name: string | null; role: string }

export interface Page<T> { items: T[]; next_cursor: string | null }

export interface Workflow {
  id: string; slug: string; name: string; description: string | null; tags: string[];
  is_paused: boolean; max_active_runs: number; created_at: string; updated_at: string;
}

export interface TaskSpec { key: string; operator: string; depends_on?: string[]; queue?: string | null }

export interface WorkflowDetail extends Workflow {
  current_version: { id: string; version: number; definition: { tasks: TaskSpec[] } } | null;
}

export interface Trigger {
  id: string; type: "cron" | "event" | "webhook"; name: string | null; is_enabled: boolean;
  cron_expression: string | null; timezone: string; next_fire_at: string | null;
  last_fired_at: string | null; event_type: string | null; webhook_url: string | null;
}

export interface Run {
  id: string; workflow_id: string; status: RunStatus; trigger_type: string;
  created_at: string; started_at: string | null; finished_at: string | null;
  admitted_at: string | null; error_message: string | null;
}

export interface TaskRun {
  id: string; task_key: string; operator: string; queue: string; status: TaskStatus;
  attempt_count: number; max_attempts: number; next_attempt_at: string | null;
  queued_at: string | null; started_at: string | null; finished_at: string | null;
  error_message: string | null;
}

export interface RunDetail extends Run { tasks: TaskRun[]; trigger_payload?: unknown }

export interface Attempt {
  id: string; attempt_number: number; status: string; worker_id: string | null;
  started_at: string; finished_at: string | null; duration_ms: number | null;
  error_type: string | null; error_message: string | null; traceback: string | null;
  metrics: { llm?: { provider: string; model: string; input_tokens: number; output_tokens: number; cost_usd?: number } };
}

export interface LogLine {
  id: number; attempt_number: number; level: "debug" | "info" | "warning" | "error";
  message: string; context: Record<string, unknown>; created_at: string;
}

export interface Overview {
  since: string; bucket: "hour" | "day"; runs: number; by_status: Record<string, number>;
  success_rate: number | null; duration_p50_seconds: number | null;
  duration_p95_seconds: number | null; active_runs: number; queued_tasks: number;
  series: { at: string; succeeded: number; failed: number; other: number }[];
  failing_workflows: { workflow_id: string; name: string; failed: number; total: number }[];
}

export interface LLMUsage {
  total_cost_usd: number; total_tokens: number; unpriced_calls: number;
  models: { provider: string; model: string; calls: number; input_tokens: number; output_tokens: number; cost_usd: number }[];
}

export interface Connection { id: string; name: string; type: string; description: string | null; created_at: string }
export interface Channel { id: string; name: string; type: string; target: string; is_enabled: boolean }
export interface Delivery {
  id: string; channel_id: string; workflow_run_id: string | null; event: string;
  status: "pending" | "sent" | "failed"; attempt_count: number; last_error: string | null;
  created_at: string; sent_at: string | null;
}
