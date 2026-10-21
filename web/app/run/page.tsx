"use client";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { RunDetail, ago, api, money } from "@/lib/api";
import { TopBar } from "@/lib/ui";

export default function Page() {
  return (
    <Suspense fallback={<TopBar />}>
      <RunPage />
    </Suspense>
  );
}

const STATUS: Record<string, string> = {
  success: "Succeeded",
  failed: "Failed",
  suspicious: "Finished, but a check failed",
  running: "Running",
  queued: "Waiting to start",
};
const KIND: Record<string, string> = { schedule: "Scheduled run", manual: "Manual run", webhook: "Webhook run", test: "Test run" };

function RunPage() {
  const id = useSearchParams().get("id") ?? "";
  const [run, setRun] = useState<RunDetail | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let t: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const r = await api<RunDetail>(`/api/runs/${id}`);
        setRun(r);
        if (r.status === "running" || r.status === "queued" || r.repair_status === "repairing") t = setTimeout(load, 1500);
      } catch (e: any) {
        setErr(e.message);
      }
    };
    if (id) load();
    return () => clearTimeout(t);
  }, [id]);

  if (!run) {
    return (
      <>
        <TopBar><Link href="/" className="crumb">Agents</Link></TopBar>
        <p className="empty">{err || "Loading…"}</p>
      </>
    );
  }

  const statusCls = run.status === "success" ? "live" : run.status === "suspicious" ? "paused" : run.status === "failed" ? "needs_attention" : "building";
  return (
    <>
      <TopBar>
        <Link href="/" className="crumb">Agents</Link>
        <Link href={`/agent/?id=${run.agent_id}`} className="crumb">{run.agent_name}</Link>
      </TopBar>
      <main className="run-page">
        <div className="run-title">
          <h1>{KIND[run.kind] ?? run.kind}{run.version ? `, v${run.version}` : ", draft"}</h1>
          <span className={`status ${statusCls}`} style={run.status === "suspicious" ? { color: "var(--warn)" } : undefined}>
            {STATUS[run.status] ?? run.status}
          </span>
        </div>
        <div className="run-facts">
          <span>Started <b>{run.started_at ? new Date(run.started_at).toLocaleString() : "not yet"}</b> ({ago(run.created_at)})</span>
          {run.duration_s !== null && <span>Took <b>{run.duration_s}s</b></span>}
          <span>AI <b>{run.ai_calls} {run.ai_calls === 1 ? "call" : "calls"}, {money(run.ai_cost)}</b></span>
          {run.repair_status === "fixed" && <span style={{ color: "var(--fixed)" }}>Claude fixed the agent after this run</span>}
          {run.repair_status === "not_fixed" && <span className="err">Couldn't be fixed automatically</span>}
          {run.repair_status === "repairing" && <span style={{ color: "var(--fixed)" }}>Claude is repairing the agent…</span>}
        </div>

        {run.error && (
          <div className="box error-box">
            <h2>What went wrong</h2>
            <pre className="block mono">{run.error}</pre>
          </div>
        )}

        <div className="box timeline">
          <h2>Steps</h2>
          {run.steps.length === 0 && <p className="hint" style={{ padding: "10px 16px" }}>No steps recorded.</p>}
          {run.steps.map((s) => (
            <div key={s.id} className="t-step" style={{ paddingLeft: 16 + s.depth * 22 }}>
              <span className={`t-icon ${s.status}`} aria-label={s.status}>{s.status === "ok" ? "✓" : s.status === "running" ? "…" : "✗"}</span>
              <span>
                {s.name}
                {s.ai_calls > 0 && <span className="tag ai" style={{ marginLeft: 8 }}>AI ×{s.ai_calls}</span>}
                {s.notes.map((n, i) => <div key={i} className="t-note">{n}</div>)}
                {s.error && <div className="t-err">{s.error}</div>}
              </span>
              <span className="t-dur">{s.duration_s !== null ? `${s.duration_s}s` : ""}</span>
            </div>
          ))}
        </div>

        {run.checks.length > 0 && (
          <div className="box">
            <h2>Checks</h2>
            <ul className="checks">
              {run.checks.map((c, i) => (
                <li key={i}>
                  <span className={`t-icon ${c.ok ? "ok" : "error"}`}>{c.ok ? "✓" : "✗"}</span>
                  <span>{c.message}{c.step && <span className="t-note"> in “{c.step}”</span>}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {run.notifications.length > 0 && (
          <div className="box">
            <h2>Messages</h2>
            <ul className="notes">
              {run.notifications.map((n, i) => (
                <li key={i} style={{ flexDirection: "column", gap: 4 }}>
                  <span style={{ display: "flex", gap: 10 }}>
                    <b>{n.subject || "(no subject)"}</b>
                    <span className="st">{n.status}</span>
                  </span>
                  <span style={{ whiteSpace: "pre-wrap", color: "var(--muted)" }}>{n.message}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {run.output !== null && run.output !== undefined && (
          <div className="box">
            <h2>Result</h2>
            <pre className="block mono">{typeof run.output === "string" ? run.output : JSON.stringify(run.output, null, 2)}</pre>
          </div>
        )}

        {run.input?.event && Object.keys(run.input.event).length > 0 && (
          <div className="box">
            <h2>Input</h2>
            <pre className="block mono">{JSON.stringify(run.input.event, null, 2)}</pre>
          </div>
        )}

        <div className="box">
          <h2>Logs</h2>
          <pre className="block mono">{run.logs || "No output."}</pre>
        </div>
      </main>
    </>
  );
}
