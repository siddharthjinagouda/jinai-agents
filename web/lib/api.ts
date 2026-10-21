// Talks to the Jinai backend. In production the web app is served by the backend, so the base is "".
const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export function getToken(): string {
  try {
    return localStorage.getItem("jinai_token") ?? "";
  } catch {
    return "";
  }
}

export function setToken(t: string) {
  try {
    localStorage.setItem("jinai_token", t);
  } catch {
    /* storage unavailable */
  }
}

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  const res = await fetch(BASE + path, {
    method: opts.method ?? (opts.body !== undefined ? "POST" : "GET"),
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${getToken()}`,
    },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401 && typeof window !== "undefined" && !location.pathname.startsWith("/login")) {
    location.href = "/login/";
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, (data as any).detail ?? `Request failed (${res.status})`);
  return data as T;
}

// ---------------------------------------------------------------- types
export type RunStatus = "queued" | "running" | "success" | "failed" | "suspicious";

export interface RecentRun {
  id: string;
  status: RunStatus;
  repair: string;
  at: string;
}

export interface RunBrief {
  id: string;
  kind: string;
  status: RunStatus;
  version: number | null;
  created_at: string;
  finished_at: string | null;
  duration_s: number | null;
  summary: string;
  ai_cost: number;
  repair_status: string;
}

export interface Health {
  runs_30d: number;
  success_30d: number;
  success_rate: number | null;
  fixed_30d: number;
  recent: RecentRun[];
  last_run: RunBrief | null;
}

export interface AgentRow {
  id: string;
  name: string;
  status: "draft" | "live" | "paused" | "needs_attention";
  trigger: string;
  trigger_type: string;
  next_run_at: string | null;
  builder_busy: boolean;
  health: Health;
  draft_dirty: boolean;
  updated_at: string;
}

export interface OutlineStep {
  name: string;
  line: number;
  depth: number;
  ai: boolean;
  checks: number;
  notifies: boolean;
  error?: boolean;
}

export interface AgentDetail extends AgentRow {
  description: string;
  schedule: string;
  timezone: string;
  webhook_url: string;
  alert_email: string;
  max_steps: number;
  max_cost_per_run: number;
  timeout_s: number;
  auto_repair: boolean;
  needed_secrets: string[];
  secrets_set: string[];
  memory: Record<string, unknown>;
  builder_activity: string;
  files: Record<string, string>;
  outline: OutlineStep[];
  syntax_errors: { file: string; line: number; message: string }[];
  live_version: number | null;
  versions: { number: number; note: string; by: string; created_at: string; live: boolean }[];
}

export interface Msg {
  id: number;
  role: "user" | "assistant" | "activity";
  text: string;
  data: Record<string, any>;
  created_at: string;
}

export interface RunStep {
  id: number;
  parent: number | null;
  depth: number;
  name: string;
  status: string;
  duration_s: number | null;
  notes: string[];
  error: string;
  ai_calls: number;
}

export interface RunDetail extends RunBrief {
  agent_id: string;
  agent_name: string;
  input: any;
  output: any;
  steps: RunStep[];
  checks: { ok: boolean; message: string; step: string | null }[];
  notifications: { message: string; subject: string; to: string; status: string }[];
  logs: string;
  error: string;
  ai_calls: number;
  started_at: string | null;
}

// ---------------------------------------------------------------- formatting
export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < -60) return "in " + span(-s);
  if (s < 45) return "just now";
  return span(s) + " ago";
}

export function until(iso: string | null | undefined): string {
  if (!iso) return "";
  const s = (new Date(iso).getTime() - Date.now()) / 1000;
  return s <= 0 ? "due now" : "in " + span(s);
}

function span(s: number): string {
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min`;
  if (s < 86400) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} d`;
}

export function money(n: number): string {
  return n < 0.01 && n > 0 ? "<$0.01" : `$${n.toFixed(2)}`;
}
