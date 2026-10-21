"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { AgentRow, ago, api, money, until } from "@/lib/api";
import { RunStrip, Status, TopBar, rate } from "@/lib/ui";

interface Summary {
  agents: number;
  working: number;
  fixed_7d: number;
  needs_you: number;
  paused: number;
  drafts: number;
  ai_cost_month: number;
  runs_24h: number;
}
interface FeedItem {
  id: number;
  agent_id: string;
  agent: string;
  kind: string;
  text: string;
  run_id: string | null;
  created_at: string;
}

const EXAMPLES = [
  "Check the pricing pages of Notion and Coda every Monday and email me what changed",
  "When a Stripe payment fails (webhook), look up the customer and draft a friendly email",
  "Every morning, read new rows in my leads sheet, research each company and summarise them",
];

const FEED_TEXT: Record<string, string> = {
  fixed: "Auto-fixed",
  needs_you: "Needs you",
  deployed: "Deployed",
  paused: "Paused",
  resumed: "Resumed",
};

export default function Home() {
  const router = useRouter();
  const [agents, setAgents] = useState<AgentRow[] | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [me, setMe] = useState<{ ai_configured: boolean; email_configured: boolean } | null>(null);
  const [request, setRequest] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function load() {
    const [a, s, f] = await Promise.all([api<AgentRow[]>("/api/agents"), api<Summary>("/api/summary"), api<FeedItem[]>("/api/events?limit=12")]);
    setAgents(a);
    setSummary(s);
    setFeed(f);
  }

  useEffect(() => {
    load().catch((e) => setError(e.message));
    api("/api/me").then(setMe).catch(() => {});
    const t = setInterval(() => load().catch(() => {}), 5000);
    return () => clearInterval(t);
  }, []);

  async function create() {
    if (request.trim().length < 5) return;
    setBusy(true);
    setError("");
    try {
      const { id } = await api<{ id: string }>("/api/agents", { body: { request } });
      router.push(`/agent/?id=${id}`);
    } catch (e: any) {
      setError(e.message);
      setBusy(false);
    }
  }

  return (
    <>
      <TopBar />
      <main className="home">
        <div>
          <div className="compose">
            <h1>What should your agent do?</h1>
            <textarea
              className="field"
              aria-label="Describe the agent"
              placeholder="Describe the job, when it should run and where results should go…"
              value={request}
              onChange={(e) => setRequest(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) create();
              }}
            />
            <div className="compose-foot">
              <div className="examples">
                {EXAMPLES.map((x) => (
                  <button key={x} className="example" onClick={() => setRequest(x)}>
                    {x.length > 46 ? x.slice(0, 44) + "…" : x}
                  </button>
                ))}
              </div>
              <button className="btn primary" disabled={busy || request.trim().length < 5} onClick={create}>
                {busy ? "Starting…" : "Build agent"}
              </button>
            </div>
            {error && <p className="err" style={{ marginTop: 8 }}>{error}</p>}
          </div>

          {summary && (
            <div className="tally" aria-label="Overview">
              <div><b>{summary.working}</b> working</div>
              <div className="needs"><b>{summary.needs_you}</b> need you</div>
              <div className="fixed"><b>{summary.fixed_7d}</b> auto-fixed this week</div>
              <div><b>{summary.runs_24h}</b> runs today</div>
              <div><b>{money(summary.ai_cost_month)}</b> AI this month</div>
            </div>
          )}

          <div className="agents" role="list">
            {agents === null ? (
              <div className="agents-empty">Loading…</div>
            ) : agents.length === 0 ? (
              <div className="agents-empty">No agents yet. Describe one above and Claude will build it.</div>
            ) : (
              agents.map((a) => (
                <div
                  key={a.id}
                  role="listitem"
                  className="agent-row"
                  style={{ cursor: "pointer" }}
                  onClick={() => router.push(`/agent/?id=${a.id}`)}
                >
                  <div style={{ minWidth: 0 }}>
                    <Link href={`/agent/?id=${a.id}`} className="name" style={{ textDecoration: "none", display: "block" }}>
                      {a.name}
                    </Link>
                    <div className="sub">
                      {a.trigger}
                      {a.next_run_at ? `, next ${until(a.next_run_at)}` : ""}
                      {a.health.last_run ? `. Last run ${ago(a.health.last_run.created_at)}` : ""}
                    </div>
                  </div>
                  <Status agent={a} />
                  <RunStrip runs={a.health.recent} link={false} />
                  <div className="rate">{rate(a.health)}</div>
                </div>
              ))
            )}
          </div>
        </div>

        <aside className="rail">
          <h2>What happened</h2>
          {feed.length === 0 ? (
            <p className="hint">Deploys, automatic fixes and problems show up here.</p>
          ) : (
            <div className="feed">
              {feed.map((f) => (
                <Link key={f.id} href={f.run_id ? `/run/?id=${f.run_id}` : `/agent/?id=${f.agent_id}`}>
                  <span className={`dot ${f.kind}`} />
                  <span>
                    <div className="who">
                      {FEED_TEXT[f.kind] ?? f.kind}: {f.agent}
                    </div>
                    <div className="what">{f.text}</div>
                    <div className="when">{ago(f.created_at)}</div>
                  </span>
                </Link>
              ))}
            </div>
          )}
          {me && !me.ai_configured && (
            <div className="notice">Claude isn't connected yet. Set ANTHROPIC_API_KEY on the server to build agents.</div>
          )}
          {me && !me.email_configured && (
            <div className="notice">Email isn't set up, so alerts are kept in each run instead of being sent. Add SMTP settings to send them.</div>
          )}
        </aside>
      </main>
    </>
  );
}
