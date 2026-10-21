"use client";
import Link from "next/link";
import type { AgentRow, RecentRun } from "./api";

export function TopBar({ children }: { children?: React.ReactNode }) {
  return (
    <header className="topbar">
      <Link href="/" className="brand" aria-label="Jinai home">
        <span className="brand-mark" aria-hidden>
          <i /><i /><i /><i />
        </span>
        Jinai
      </Link>
      {children}
      <span className="spacer" />
    </header>
  );
}

const STATUS_TEXT: Record<string, string> = {
  live: "Live",
  draft: "Not deployed",
  paused: "Paused",
  needs_attention: "Needs you",
};

export function Status({ agent }: { agent: Pick<AgentRow, "status" | "builder_busy"> }) {
  if (agent.builder_busy) return <span className="status building">Building</span>;
  return <span className={`status ${agent.status}`}>{STATUS_TEXT[agent.status] ?? agent.status}</span>;
}

/** One tick per run, oldest to newest: green ok, red failed, amber check failed, purple auto-fixed. */
export function RunStrip({ runs, slots = 30, link = true }: { runs: RecentRun[]; slots?: number; link?: boolean }) {
  const pad = Math.max(0, slots - runs.length);
  const label = (r: RecentRun) =>
    r.repair === "fixed" ? "failed, then auto-fixed" : r.status === "suspicious" ? "check failed" : r.status;
  return (
    <div className="strip" aria-label={`Last ${runs.length} runs`}>
      {Array.from({ length: pad }).map((_, i) => (
        <span key={`e${i}`} className="pad" />
      ))}
      {runs.map((r) => {
        const cls = r.repair === "fixed" ? "fixed" : r.status;
        const title = `${label(r)}, ${new Date(r.at).toLocaleString()}`;
        return link ? (
          <a key={r.id} href={`/run/?id=${r.id}`} className={cls} title={title} aria-label={title} onClick={(e) => e.stopPropagation()} />
        ) : (
          <span key={r.id} className={cls} title={title} />
        );
      })}
    </div>
  );
}

export function rate(h: AgentRow["health"]): string {
  if (h.success_rate === null) return "No runs yet";
  return `${Math.round(h.success_rate * 100)}% of ${h.runs_30d} runs`;
}
