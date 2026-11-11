"use client";
import { markdown } from "@codemirror/lang-markdown";
import { python } from "@codemirror/lang-python";
import { EditorSelection } from "@codemirror/state";
import type { EditorView } from "@codemirror/view";
import CodeMirror from "@uiw/react-codemirror";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AgentDetail, Msg, RunBrief, ago, api, money, until } from "@/lib/api";
import { RunStrip, Status, TopBar } from "@/lib/ui";

type Tab = "steps" | "code" | "store" | "memory" | "approvals" | "analytics" | "settings";

interface StoreRow { id: string; created_at: string; [k: string]: unknown }
interface MemoryItem { id: string; text: string; tags: string[]; created_at: string }
interface Approval { id: string; run_id: string; agent_id: string; message: string; context: Record<string, unknown> | null; created_at: string; expires_at: string | null }
interface AnalyticsDay { date: string; total: number; success: number; success_rate: number; cost: number; avg_duration_s: number | null }
interface Analytics { total_runs: number; success_rate: number | null; total_cost: number; avg_cost_per_run: number | null; daily: AnalyticsDay[]; step_breakdown: { name: string; total: number; failures: number; failure_rate: number }[] }

export default function Page() {
  return (
    <Suspense fallback={<TopBar />}>
      <AgentPage />
    </Suspense>
  );
}

function AgentPage() {
  const id = useSearchParams().get("id") ?? "";
  const router = useRouter();
  const [agent, setAgent] = useState<AgentDetail | null>(null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [runs, setRuns] = useState<RunBrief[]>([]);
  const [tab, setTab] = useState<Tab>("steps");
  const [storeTables, setStoreTables] = useState<{ name: string; count: number }[]>([]);
  const [storeTable, setStoreTable] = useState("");
  const [storeRows, setStoreRows] = useState<StoreRow[]>([]);
  const [memoryItems, setMemoryItems] = useState<MemoryItem[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [analytics, setAnalytics] = useState<Analytics | null>(null);
  const [files, setFiles] = useState<Record<string, string>>({});
  const [activeFile, setActiveFile] = useState("agent.py");
  const [localDirty, setLocalDirty] = useState(false);
  const [saveState, setSaveState] = useState("");
  const [outline, setOutline] = useState<AgentDetail["outline"]>([]);
  const [syntax, setSyntax] = useState<AgentDetail["syntax_errors"]>([]);
  const [action, setAction] = useState("");
  const [error, setError] = useState("");
  const lastMsg = useRef(0);
  const dirtyRef = useRef(false);
  const viewRef = useRef<EditorView | null>(null);
  const pendingLine = useRef<number | null>(null);

  const loadTab = useCallback(async (t: Tab) => {
    if (!id) return;
    if (t === "store") {
      const r = await api<{ tables: { name: string; count: number }[] }>(`/api/agents/${id}/store`);
      setStoreTables(r.tables);
      if (r.tables.length && !storeTable) setStoreTable(r.tables[0].name);
    } else if (t === "memory") {
      const r = await api<{ items: MemoryItem[] }>(`/api/agents/${id}/memory`);
      setMemoryItems(r.items);
    } else if (t === "approvals") {
      const r = await api<Approval[]>(`/api/approvals`);
      setApprovals(r.filter((a: Approval) => a.agent_id === id));
    } else if (t === "analytics") {
      const r = await api<Analytics>(`/api/agents/${id}/analytics`);
      setAnalytics(r);
    }
  }, [id, storeTable]);

  const load = useCallback(async () => {
    if (!id) return;
    const [d, m, r] = await Promise.all([
      api<AgentDetail>(`/api/agents/${id}`),
      api<Msg[]>(`/api/agents/${id}/messages?after=${lastMsg.current}`),
      api<RunBrief[]>(`/api/agents/${id}/runs?limit=40`),
    ]);
    setAgent(d);
    setRuns(r);
    if (m.length) {
      lastMsg.current = m[m.length - 1].id;
      setMsgs((prev) => [...prev, ...m]);
    }
    if (!dirtyRef.current) {
      setFiles(d.files);
      setOutline(d.outline);
      setSyntax(d.syntax_errors);
    }
  }, [id]);

  useEffect(() => {
    load().catch((e) => setError(e.message));
  }, [load]);

  useEffect(() => {
    loadTab(tab).catch(() => {});
  }, [tab, loadTab]);

  const busy = agent?.builder_busy ?? false;
  useEffect(() => {
    const t = setInterval(() => load().catch(() => {}), busy ? 1500 : 4000);
    return () => clearInterval(t);
  }, [load, busy]);

  // autosave the user's edits (and save immediately before a test run or deploy)
  const filesRef = useRef(files);
  filesRef.current = files;
  const editVer = useRef(0);

  const saveNow = useCallback(async () => {
    if (!dirtyRef.current) return;
    const ver = editVer.current;
    try {
      const r = await api<{ outline: AgentDetail["outline"]; syntax_errors: AgentDetail["syntax_errors"] }>(
        `/api/agents/${id}/files`,
        { method: "PUT", body: { files: filesRef.current } },
      );
      setOutline(r.outline);
      setSyntax(r.syntax_errors);
      if (editVer.current === ver) {
        dirtyRef.current = false;
        setLocalDirty(false);
        setSaveState("Saved");
      }
    } catch (e: any) {
      setSaveState(e.message);
      throw e;
    }
  }, [id]);

  useEffect(() => {
    if (!localDirty) return;
    setSaveState("Unsaved changes");
    const t = setTimeout(() => saveNow().catch(() => {}), 700);
    return () => clearTimeout(t);
  }, [files, localDirty, saveNow]);

  function editFile(content: string) {
    dirtyRef.current = true;
    editVer.current += 1;
    setLocalDirty(true);
    setFiles((f) => ({ ...f, [activeFile]: content }));
  }

  function jumpTo(line: number) {
    setActiveFile("agent.py");
    setTab("code");
    pendingLine.current = line;
    setTimeout(() => {
      const v = viewRef.current;
      if (!v || pendingLine.current === null) return;
      const l = v.state.doc.line(Math.min(pendingLine.current, v.state.doc.lines));
      v.dispatch({ selection: EditorSelection.cursor(l.from), scrollIntoView: true });
      v.focus();
      pendingLine.current = null;
    }, 60);
  }

  async function act(label: string, fn: () => Promise<unknown>) {
    setAction(label);
    setError("");
    try {
      await saveNow();
      await fn();
      await load();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setAction("");
    }
  }

  if (!agent) {
    return (
      <>
        <TopBar>
          <Link href="/" className="crumb">Agents</Link>
        </TopBar>
        <p className="empty">{error || "Loading…"}</p>
      </>
    );
  }

  const canDeploy = (agent.draft_dirty || !agent.live_version) && !!files["agent.py"] && syntax.length === 0 && !busy;
  const banner =
    agent.status === "needs_attention"
      ? "needs"
      : agent.status === "draft" && files["agent.py"] && !busy
        ? "draft"
        : null;

  return (
    <div className="agent-page">
      <TopBar>
        <Link href="/" className="crumb">Agents</Link>
      </TopBar>
      <div className="agent-head">
        <div className="title">
          <h1>{agent.name}</h1>
          <div className="meta">
            <Status agent={agent} />
            <span>{agent.trigger}{agent.next_run_at ? `, next ${until(agent.next_run_at)}` : ""}</span>
            {agent.live_version && <span>Live version v{agent.live_version}{agent.draft_dirty ? " (draft has changes)" : ""}</span>}
            <RunStrip runs={agent.health.recent} slots={20} />
          </div>
        </div>
        <div className="actions">
          <button
            className="btn"
            disabled={!!action || busy || !files["agent.py"] || syntax.length > 0}
            onClick={() => act("test", () => api(`/api/agents/${id}/test`, { body: {} }))}
          >
            {action === "test" ? "Testing…" : "Test run"}
          </button>
          {agent.live_version && agent.status !== "paused" && (
            <button className="btn" disabled={!!action} onClick={() => act("run", () => api(`/api/agents/${id}/run`, { body: {} }))}>
              {action === "run" ? "Starting…" : "Run now"}
            </button>
          )}
          {agent.status === "live" && (
            <button className="btn ghost" disabled={!!action} onClick={() => act("pause", () => api(`/api/agents/${id}/pause`, { body: {} }))}>
              Pause
            </button>
          )}
          {(agent.status === "paused" || agent.status === "needs_attention") && (
            <button className="btn" disabled={!!action} onClick={() => act("resume", () => api(`/api/agents/${id}/resume`, { body: {} }))}>
              Resume
            </button>
          )}
          {(agent.draft_dirty || !agent.live_version) && (
            <button
              className="btn primary"
              disabled={!!action || !canDeploy}
              onClick={() => act("deploy", () => api(`/api/agents/${id}/deploy`, { body: {} }))}
            >
              {action === "deploy" ? "Deploying…" : agent.live_version ? "Deploy changes" : "Deploy"}
            </button>
          )}
        </div>
      </div>
      {banner === "needs" && (
        <div className="banner needs">
          This agent is paused because a problem couldn't be fixed automatically. The chat explains what happened.
        </div>
      )}
      {banner === "draft" && (
        <div className="banner draft">Not running yet. Check the steps and the test result, then deploy.</div>
      )}
      {error && <div className="banner needs">{error}</div>}

      <div className="workbench">
        <ChatPane id={id} msgs={msgs} busy={busy} activity={agent.builder_activity} onSent={load} />

        <section className="pane editor-pane" aria-label="Agent">
          <div className="pane-head" style={{ padding: "0 8px", overflowX: "auto" }} role="tablist">
            {(["steps","code","store","memory","approvals","analytics","settings"] as Tab[]).map((t) => (
              <button key={t} role="tab" className="tab" aria-selected={tab === t} onClick={() => setTab(t)}
                style={{ whiteSpace: "nowrap" }}>
                {t === "steps" ? "Steps" : t === "code" ? "Code" : t === "store" ? "Store" : t === "memory" ? "Memory" : t === "approvals" ? "Approvals" : t === "analytics" ? "Analytics" : "Settings"}
              </button>
            ))}
          </div>
          {tab === "steps" && <StepsView agent={agent} outline={outline} hasCode={!!files["agent.py"]} busy={busy} onJump={jumpTo} />}
          {tab === "store" && (
            <StoreView agentId={id} tables={storeTables} activeTable={storeTable} rows={storeRows}
              onTableChange={async (t) => { setStoreTable(t); const r = await api<{ table: string; total: number; rows: StoreRow[] }>(`/api/agents/${id}/store/${t}`); setStoreRows(r.rows); }}
              onLoad={async () => { if (storeTable) { const r = await api<{ table: string; total: number; rows: StoreRow[] }>(`/api/agents/${id}/store/${storeTable}`); setStoreRows(r.rows); } }} />
          )}
          {tab === "memory" && <MemoryView agentId={id} items={memoryItems} onDelete={async (itemId) => { await api(`/api/agents/${id}/memory/${itemId}`, { method: "DELETE" }); setMemoryItems((x) => x.filter((i) => i.id !== itemId)); }} />}
          {tab === "approvals" && <ApprovalsView approvals={approvals} onRespond={async (apId, approved) => { await api(`/api/approvals/${apId}/respond`, { body: { approved } }); await loadTab("approvals"); }} />}
          {tab === "analytics" && <AnalyticsView analytics={analytics} />}
          {tab === "code" && (
            <>
              <div className="files" role="tablist" aria-label="Files">
                {Object.keys(files).sort(fileOrder).map((f) => (
                  <button key={f} role="tab" className="file" aria-selected={activeFile === f} onClick={() => setActiveFile(f)}>
                    {f}
                  </button>
                ))}
                <NewFile onAdd={(name) => { setFiles((x) => ({ ...x, [name]: "" })); setActiveFile(name); dirtyRef.current = true; editVer.current += 1; setLocalDirty(true); }} />
              </div>
              <div className="editor-wrap">
                {files[activeFile] !== undefined ? (
                  <CodeMirror
                    value={files[activeFile]}
                    height="100%"
                    theme={prefersDark() ? "dark" : "light"}
                    extensions={activeFile.endsWith(".py") ? [python()] : [markdown()]}
                    editable={!busy}
                    onCreateEditor={(v) => (viewRef.current = v)}
                    onChange={editFile}
                    basicSetup={{ foldGutter: false }}
                  />
                ) : (
                  <p className="empty">{busy ? "Claude is writing the code…" : "No files yet. Describe the agent in the chat."}</p>
                )}
              </div>
              <div className="savebar">
                {busy ? <span>Claude is editing; editing is paused until it finishes.</span> : <span>{saveState}</span>}
                <span className="spacer" />
                {syntax.length > 0 && (
                  <button className="btn ghost small err" onClick={() => jumpTo(syntax[0].line)}>
                    Syntax error in {syntax[0].file}, line {syntax[0].line}: {syntax[0].message}
                  </button>
                )}
              </div>
            </>
          )}
          {tab === "settings" && <SettingsView agent={agent} reload={load} onDeleted={() => router.push("/")} />}
        </section>

        <RunsPane id={id} runs={runs} memory={agent.memory} reload={load} />
      </div>
    </div>
  );
}

function fileOrder(a: string, b: string) {
  const rank = (f: string) => (f === "agent.py" ? 0 : f.startsWith("prompts/") ? 1 : f.endsWith(".py") ? 2 : 3);
  return rank(a) - rank(b) || a.localeCompare(b);
}

function prefersDark() {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches;
}

function NewFile({ onAdd }: { onAdd: (n: string) => void }) {
  return (
    <button
      className="file"
      onClick={() => {
        const n = prompt("New file (e.g. prompts/summary.md)");
        if (n && /^[\w\-/]+\.(py|md|txt|json|csv|yaml|yml)$/.test(n)) onAdd(n);
      }}
    >
      + file
    </button>
  );
}

// ------------------------------------------------------------------ chat
function ChatPane({ id, msgs, busy, activity, onSent }: { id: string; msgs: Msg[]; busy: boolean; activity: string; onSent: () => void }) {
  const [text, setText] = useState("");
  const [err, setErr] = useState("");
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => end.current?.scrollIntoView({ block: "end" }), [msgs.length, busy]);

  async function send() {
    if (!text.trim() || busy) return;
    setErr("");
    try {
      await api(`/api/agents/${id}/messages`, { body: { text } });
      setText("");
      onSent();
    } catch (e: any) {
      setErr(e.message);
    }
  }

  return (
    <section className="pane chat-pane" aria-label="Chat with Claude">
      <div className="pane-head"><h2>Chat</h2></div>
      <div className="pane-body">
        <div className="chat">
          {msgs.map((m) =>
            m.role === "activity" ? (
              <div key={m.id} className={`activity ${m.data?.status ?? ""}`}>
                <span>
                  {m.text}
                  {(m.data?.run_id || m.data?.test_run_id) && (
                    <> <Link href={`/run/?id=${m.data.run_id ?? m.data.test_run_id}`}>details</Link></>
                  )}
                </span>
              </div>
            ) : (
              <div key={m.id} className={`bubble ${m.role}`}>{m.text}</div>
            ),
          )}
          {busy && <div className="thinking">{activity || "Working…"}</div>}
          <div ref={end} />
        </div>
      </div>
      <div className="chat-input">
        <textarea
          className="field"
          aria-label="Message Claude"
          placeholder={busy ? "Claude is working…" : "Ask for a change, or ask why something failed"}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        <div className="row">
          <span className="hint">{err ? <span className="err">{err}</span> : "Enter to send, Shift+Enter for a new line"}</span>
          <button className="btn primary small" disabled={busy || !text.trim()} onClick={send}>Send</button>
        </div>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ steps
function StepsView({ agent, outline, hasCode, busy, onJump }: {
  agent: AgentDetail; outline: AgentDetail["outline"]; hasCode: boolean; busy: boolean; onJump: (l: number) => void;
}) {
  if (!hasCode) {
    return <div className="pane-body"><p className="empty">{busy ? "Claude is planning the agent…" : "Describe what the agent should do in the chat."}</p></div>;
  }
  let n = 0;
  const notifies = outline.some((s) => s.notifies);
  return (
    <div className="pane-body">
      <div className="steps">
        <div className="steps-trigger">
          <span>Runs</span>
          <b>{agent.trigger}</b>
          {agent.trigger_type === "webhook" && <span className="hint">when its webhook URL is called</span>}
        </div>
        {outline.length === 0 && <p className="hint" style={{ padding: "12px 0" }}>The code has no step() markers yet, so there's nothing to outline.</p>}
        {outline.map((s, i) => {
          const sub = s.depth > 0;
          if (!sub) n += 1;
          return (
            <div key={i} className={`step-item${sub ? " sub" : ""}`}>
              <span className="step-num" aria-hidden>{sub ? "" : n}</span>
              <div className="step-card">
                <button onClick={() => onJump(s.line)} className={s.error ? "err" : ""}>{s.name}</button>
                {(s.ai || s.checks > 0 || s.notifies) && (
                  <div className="step-tags">
                    {s.ai && <span className="tag ai">Uses AI</span>}
                    {s.checks > 0 && <span className="tag check">{s.checks} {s.checks === 1 ? "check" : "checks"}</span>}
                    {s.notifies && <span className="tag notify">Sends a message</span>}
                  </div>
                )}
              </div>
            </div>
          );
        })}
        {notifies && (
          <p className="hint" style={{ marginTop: 14 }}>
            Messages go to {agent.alert_email || "your email"}, and only when every check passes.
          </p>
        )}
        {agent.needed_secrets.filter((s) => !agent.secrets_set.includes(s)).length > 0 && (
          <p className="err" style={{ marginTop: 10, fontSize: 14 }}>
            Needs {agent.needed_secrets.filter((s) => !agent.secrets_set.includes(s)).join(", ")}. Add it in Settings.
          </p>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ settings
const TZS = ["UTC", "Asia/Kolkata", "Europe/London", "Europe/Berlin", "America/New_York", "America/Los_Angeles", "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney"];
const PRESETS: [string, string][] = [
  ["Every 15 min", "*/15 * * * *"],
  ["Hourly", "0 * * * *"],
  ["Daily 09:00", "0 9 * * *"],
  ["Weekdays 09:00", "0 9 * * 1-5"],
  ["Mondays 09:00", "0 9 * * 1"],
];

function SettingsView({ agent, reload, onDeleted }: { agent: AgentDetail; reload: () => Promise<void>; onDeleted: () => void }) {
  const [form, setForm] = useState({
    name: agent.name, schedule: agent.schedule || "0 9 * * *", timezone: agent.timezone, alert_email: agent.alert_email,
    max_steps: agent.max_steps, max_cost_per_run: agent.max_cost_per_run, timeout_s: agent.timeout_s,
  });
  const [msg, setMsg] = useState("");
  const [secretName, setSecretName] = useState("");
  const [secretValue, setSecretValue] = useState("");
  const tzs = useMemo(() => (TZS.includes(agent.timezone) ? TZS : [agent.timezone, ...TZS]), [agent.timezone]);

  async function save(body: Record<string, unknown>) {
    setMsg("");
    try {
      await api(`/api/agents/${agent.id}/settings`, { method: "PUT", body });
      await reload();
      setMsg("Saved");
    } catch (e: any) {
      setMsg(e.message);
    }
  }

  async function putSecret(name: string, value: string | null) {
    try {
      await api(`/api/agents/${agent.id}/secrets`, { method: "PUT", body: { name, value } });
      setSecretName("");
      setSecretValue("");
      await reload();
    } catch (e: any) {
      setMsg(e.message);
    }
  }

  const secretNames = Array.from(new Set([...agent.needed_secrets, ...agent.secrets_set]));
  return (
    <div className="pane-body">
      <div className="settings">
        <section>
          <h2>Name</h2>
          <div className="copy">
            <input className="field" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <button className="btn" onClick={() => save({ name: form.name })}>Save</button>
          </div>
        </section>

        <section>
          <h2>When it runs</h2>
          <div className="choice" role="group" aria-label="Trigger">
            {(["schedule", "webhook", "manual"] as const).map((t) => (
              <button key={t} aria-pressed={agent.trigger_type === t} onClick={() => save({ trigger_type: t })}>
                {t === "schedule" ? "On a schedule" : t === "webhook" ? "When called (webhook)" : "Only when I run it"}
              </button>
            ))}
          </div>
          {agent.trigger_type === "schedule" && (
            <>
              <div className="choice">
                {PRESETS.map(([label, c]) => (
                  <button key={c} aria-pressed={form.schedule === c} onClick={() => setForm({ ...form, schedule: c })}>{label}</button>
                ))}
              </div>
              <div className="row2">
                <label>Schedule (cron)<input className="field mono" value={form.schedule} onChange={(e) => setForm({ ...form, schedule: e.target.value })} /></label>
                <label>Time zone
                  <select className="field" value={form.timezone} onChange={(e) => setForm({ ...form, timezone: e.target.value })}>
                    {tzs.map((z) => <option key={z}>{z}</option>)}
                  </select>
                </label>
              </div>
              <div><button className="btn" onClick={() => save({ schedule: form.schedule, timezone: form.timezone })}>Save schedule</button></div>
            </>
          )}
          {agent.trigger_type === "webhook" && (
            <label>Webhook URL. POST JSON to it; add ?wait=30 to get the result back.
              <div className="copy">
                <input className="field mono" readOnly value={agent.webhook_url} onFocus={(e) => e.target.select()} />
                <button className="btn" onClick={() => navigator.clipboard?.writeText(agent.webhook_url)}>Copy</button>
              </div>
            </label>
          )}
        </section>

        <section>
          <h2>Alerts</h2>
          <label>Send messages and problem alerts to
            <div className="copy">
              <input className="field" type="email" value={form.alert_email} placeholder="you@company.com" onChange={(e) => setForm({ ...form, alert_email: e.target.value })} />
              <button className="btn" onClick={() => save({ alert_email: form.alert_email })}>Save</button>
            </div>
          </label>
          <label className="check-line">
            <input type="checkbox" checked={agent.auto_repair} onChange={(e) => save({ auto_repair: e.target.checked })} />
            Let Claude fix this agent automatically when a run fails
          </label>
        </section>

        <section>
          <h2>Secrets</h2>
          <p className="hint">API keys and passwords the code reads with secret("NAME"). Stored encrypted; never shown again.</p>
          {secretNames.map((n) => (
            <div key={n} className="secret-row">
              <span className="mono">{n}</span>
              {agent.secrets_set.includes(n) ? <span className="ok">Set</span> : <span className="missing">Missing</span>}
              {agent.secrets_set.includes(n) ? (
                <button className="btn small ghost" onClick={() => putSecret(n, null)}>Remove</button>
              ) : (
                <button className="btn small" onClick={() => setSecretName(n)}>Add</button>
              )}
            </div>
          ))}
          <div className="secret-row">
            <input className="field mono" placeholder="NAME" value={secretName} onChange={(e) => setSecretName(e.target.value.toUpperCase())} />
            <input className="field" type="password" placeholder="Value" value={secretValue} onChange={(e) => setSecretValue(e.target.value)} />
            <button className="btn" disabled={!secretName || !secretValue} onClick={() => putSecret(secretName, secretValue)}>Save secret</button>
          </div>
        </section>

        <section>
          <h2>Limits per run</h2>
          <div className="row3">
            <label>AI steps<input className="field" type="number" min={1} max={100} value={form.max_steps} onChange={(e) => setForm({ ...form, max_steps: +e.target.value })} /></label>
            <label>AI spend (USD)<input className="field" type="number" step="0.05" min={0} value={form.max_cost_per_run} onChange={(e) => setForm({ ...form, max_cost_per_run: +e.target.value })} /></label>
            <label>Time limit (s)<input className="field" type="number" min={10} max={1800} value={form.timeout_s} onChange={(e) => setForm({ ...form, timeout_s: +e.target.value })} /></label>
          </div>
          <div><button className="btn" onClick={() => save({ max_steps: form.max_steps, max_cost_per_run: form.max_cost_per_run, timeout_s: form.timeout_s })}>Save limits</button></div>
        </section>

        <section>
          <h2>Versions</h2>
          {agent.versions.length === 0 && <p className="hint">Nothing deployed yet.</p>}
          {agent.versions.map((v) => (
            <div key={v.number} className="secret-row" style={{ gridTemplateColumns: "60px 1fr auto" }}>
              <b>v{v.number}</b>
              <span className="hint" style={{ fontSize: 13 }}>
                {v.note || (v.by === "repair" ? "Auto-fix" : "Deployed")}, {ago(v.created_at)}{v.by === "repair" ? ", by auto-repair" : ""}
              </span>
              {v.live ? <span className="ok" style={{ fontSize: 13 }}>Live</span> : (
                <button className="btn small" onClick={() => api(`/api/agents/${agent.id}/rollback`, { body: { number: v.number } }).then(reload)}>
                  Roll back to this
                </button>
              )}
            </div>
          ))}
        </section>

        <section>
          <h2>Delete</h2>
          <div>
            <button
              className="btn danger"
              onClick={() => {
                if (confirm(`Delete "${agent.name}" and all its runs?`)) api(`/api/agents/${agent.id}`, { method: "DELETE" }).then(onDeleted);
              }}
            >
              Delete agent
            </button>
          </div>
        </section>
        {msg && <p className={msg === "Saved" ? "hint" : "err"}>{msg}</p>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ runs
function RunsPane({ id, runs, memory, reload }: { id: string; runs: RunBrief[]; memory: Record<string, unknown>; reload: () => Promise<void> }) {
  const kindText: Record<string, string> = { schedule: "Scheduled", manual: "Manual", webhook: "Webhook", test: "Test" };
  return (
    <section className="pane runs-pane" aria-label="Runs">
      <div className="pane-head"><h2>Runs</h2></div>
      <div className="pane-body">
        {runs.length === 0 ? (
          <p className="empty">Runs and test runs appear here.</p>
        ) : (
          <div className="runs">
            {runs.map((r) => (
              <Link key={r.id} href={`/run/?id=${r.id}`} className={`run${r.kind === "test" ? " test" : ""}`}>
                <span className={`bar ${r.status}`} />
                <span style={{ minWidth: 0 }}>
                  <span className="top">
                    <b>{kindText[r.kind] ?? r.kind}{r.version ? ` v${r.version}` : ""}</b>
                    <span className="when">{ago(r.created_at)}</span>
                  </span>
                  <span className="sum">
                    {r.repair_status === "fixed" && <span className="fixtag">Auto-fixed. </span>}
                    {r.repair_status === "repairing" && <span className="fixtag">Repairing… </span>}
                    {r.summary}
                    {r.ai_cost > 0 ? `, AI ${money(r.ai_cost)}` : ""}
                  </span>
                </span>
              </Link>
            ))}
          </div>
        )}
      </div>
      <div className="memory">
        <h3>
          What it remembers
          {Object.keys(memory).length > 0 && (
            <button className="btn ghost small" onClick={() => confirm("Clear the agent's memory? Its next run starts fresh.") && api(`/api/agents/${id}/memory`, { method: "DELETE" }).then(reload)}>
              Clear
            </button>
          )}
        </h3>
        {Object.keys(memory).length === 0 ? <p className="hint">Nothing yet. Values saved with state.set() appear after a successful run.</p> : <pre>{JSON.stringify(memory, null, 1)}</pre>}
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ store
function StoreView({ agentId, tables, activeTable, rows, onTableChange, onLoad }: {
  agentId: string; tables: { name: string; count: number }[]; activeTable: string;
  rows: StoreRow[]; onTableChange: (t: string) => void; onLoad: () => void;
}) {
  const cols = rows.length > 0 ? Array.from(new Set(rows.flatMap((r) => Object.keys(r)))) : [];
  return (
    <div className="pane-body">
      {tables.length === 0 ? (
        <p className="empty">No store tables yet. Use <code>store.insert("tablename", …)</code> from an agent run.</p>
      ) : (
        <>
          <div className="files" role="tablist" style={{ marginBottom: 8 }}>
            {tables.map((t) => (
              <button key={t.name} role="tab" className="file" aria-selected={activeTable === t.name}
                onClick={() => onTableChange(t.name)}>
                {t.name} <span style={{ opacity: 0.5, fontSize: 11 }}>({t.count})</span>
              </button>
            ))}
          </div>
          {rows.length === 0 ? (
            <p className="hint">Table is empty.</p>
          ) : (
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                <thead>
                  <tr>{cols.map((c) => <th key={c} style={{ padding: "6px 8px", textAlign: "left", borderBottom: "1px solid var(--border)" }}>{c}</th>)}</tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.id} style={{ borderBottom: "1px solid var(--border)" }}>
                      {cols.map((c) => <td key={c} style={{ padding: "5px 8px", maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{JSON.stringify(row[c] ?? "")}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ memory
function MemoryView({ agentId, items, onDelete }: { agentId: string; items: MemoryItem[]; onDelete: (id: string) => void }) {
  return (
    <div className="pane-body">
      {items.length === 0 ? (
        <p className="empty">No memories yet. Use <code>memory.remember("fact")</code> from an agent run.</p>
      ) : (
        <div>
          {items.map((item) => (
            <div key={item.id} style={{ display: "flex", alignItems: "flex-start", gap: 10, padding: "8px 0", borderBottom: "1px solid var(--border)" }}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 14 }}>{item.text}</div>
                {item.tags?.length > 0 && <div style={{ fontSize: 11, opacity: 0.5, marginTop: 2 }}>{item.tags.join(", ")}</div>}
              </div>
              <button className="btn ghost small" onClick={() => onDelete(item.id)}>Forget</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ approvals
function ApprovalsView({ approvals, onRespond }: { approvals: Approval[]; onRespond: (id: string, approved: boolean) => void }) {
  return (
    <div className="pane-body">
      {approvals.length === 0 ? (
        <p className="empty">No pending approvals. Use <code>wait_for_human("message")</code> from an agent run to pause and ask here.</p>
      ) : (
        <div>
          {approvals.map((ap) => (
            <div key={ap.id} style={{ padding: "12px 0", borderBottom: "1px solid var(--border)" }}>
              <div style={{ fontWeight: 600, marginBottom: 4 }}>{ap.message}</div>
              {ap.context && <pre style={{ fontSize: 12, opacity: 0.7, margin: "4px 0" }}>{JSON.stringify(ap.context, null, 2)}</pre>}
              <div style={{ fontSize: 12, opacity: 0.5, marginBottom: 8 }}>
                Run {ap.run_id} · {ap.expires_at ? `Expires ${new Date(ap.expires_at).toLocaleString()}` : ""}
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button className="btn primary small" onClick={() => onRespond(ap.id, true)}>Approve</button>
                <button className="btn ghost small" onClick={() => onRespond(ap.id, false)}>Reject</button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ analytics
function AnalyticsView({ analytics }: { analytics: Analytics | null }) {
  if (!analytics) return <div className="pane-body"><p className="empty">Loading…</p></div>;
  const { total_runs, success_rate, total_cost, avg_cost_per_run, daily, step_breakdown } = analytics;
  const pct = (n: number | null) => n == null ? "—" : `${Math.round(n * 100)}%`;
  const usd = (n: number | null) => n == null ? "—" : `$${n.toFixed(4)}`;
  return (
    <div className="pane-body">
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12, marginBottom: 24 }}>
        {[
          ["Runs (30d)", String(total_runs)],
          ["Success rate", pct(success_rate)],
          ["Total AI cost", usd(total_cost)],
          ["Avg cost / run", usd(avg_cost_per_run)],
        ].map(([label, val]) => (
          <div key={label} style={{ padding: "12px 16px", border: "1px solid var(--border)", borderRadius: 8 }}>
            <div style={{ fontSize: 11, opacity: 0.5, marginBottom: 4 }}>{label}</div>
            <div style={{ fontSize: 22, fontWeight: 700 }}>{val}</div>
          </div>
        ))}
      </div>

      {daily.length > 0 && (
        <>
          <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>Daily runs (last 30 days)</h3>
          <div style={{ display: "flex", alignItems: "flex-end", gap: 2, height: 80, marginBottom: 20 }}>
            {daily.map((d) => {
              const h = Math.max(4, Math.round((d.total / Math.max(...daily.map((x) => x.total), 1)) * 80));
              const col = d.success_rate >= 0.9 ? "var(--ok, #22c55e)" : d.success_rate >= 0.5 ? "#f59e0b" : "#ef4444";
              return (
                <div key={d.date} title={`${d.date}: ${d.total} runs, ${pct(d.success_rate)} success`}
                  style={{ flex: 1, height: h, background: col, borderRadius: 2, minWidth: 2, cursor: "default" }} />
              );
            })}
          </div>
        </>
      )}

      {step_breakdown.length > 0 && (
        <>
          <h3 style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>Step failure breakdown</h3>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr>{["Step", "Runs", "Failures", "Fail rate"].map((h) => <th key={h} style={{ textAlign: "left", padding: "4px 8px", opacity: 0.6 }}>{h}</th>)}</tr>
            </thead>
            <tbody>
              {step_breakdown.map((s) => (
                <tr key={s.name} style={{ borderTop: "1px solid var(--border)" }}>
                  <td style={{ padding: "4px 8px" }}>{s.name}</td>
                  <td style={{ padding: "4px 8px" }}>{s.total}</td>
                  <td style={{ padding: "4px 8px", color: s.failures > 0 ? "#ef4444" : undefined }}>{s.failures}</td>
                  <td style={{ padding: "4px 8px" }}>{pct(s.failure_rate)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
