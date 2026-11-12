"use client";
import { useCallback, useRef, useState, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  addEdge,
  useNodesState,
  useEdgesState,
  Handle,
  Position,
  type NodeProps,
  type Connection,
  type Node,
  type Edge,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { TopBar } from "@/lib/ui";
import { api } from "@/lib/api";

// ─── flow schema types ────────────────────────────────────────────────────────
export interface FlowNode {
  id: string;
  type: string;
  label: string;
  config: Record<string, string>;
}
export interface FlowEdge {
  from: string;
  to: string;
  condition?: string;
}
export interface FlowSchema {
  nodes: FlowNode[];
  edges: FlowEdge[];
}

// ─── node type definitions ────────────────────────────────────────────────────
const NODE_TYPES_META = [
  { type: "trigger",   label: "Trigger",    color: "#6c73ff", icon: "⚡", desc: "Schedule or webhook" },
  { type: "action",    label: "Action",     color: "#34d399", icon: "▶", desc: "Run code / call API" },
  { type: "ai",        label: "AI Step",    color: "#f59e0b", icon: "✦", desc: "Claude reasoning" },
  { type: "condition", label: "Condition",  color: "#f87171", icon: "◇", desc: "Branch on result" },
  { type: "notify",    label: "Notify",     color: "#a78bfa", icon: "✉", desc: "Email / Slack alert" },
  { type: "wait",      label: "Wait",       color: "#64748b", icon: "⏳", desc: "Pause for approval" },
  { type: "agent",     label: "Sub-agent",  color: "#0ea5e9", icon: "🤖", desc: "Call another agent" },
];

const COLOR: Record<string, string> = Object.fromEntries(NODE_TYPES_META.map((n) => [n.type, n.color]));
const ICON: Record<string, string>  = Object.fromEntries(NODE_TYPES_META.map((n) => [n.type, n.icon]));

// ─── custom node component ────────────────────────────────────────────────────
function FlowNodeCard({ data, selected }: NodeProps) {
  const d = data as { label: string; type: string; config: Record<string, string>; onEdit: (id: string) => void; id: string };
  const color = COLOR[d.type] ?? "#6c73ff";
  return (
    <div
      style={{
        background: "var(--surface, #1a1d27)",
        border: `2px solid ${selected ? color : "var(--border, #2d3148)"}`,
        borderRadius: 10,
        minWidth: 160,
        maxWidth: 220,
        boxShadow: selected ? `0 0 0 3px ${color}33` : "0 2px 8px #0004",
        cursor: "pointer",
        fontFamily: "system-ui, sans-serif",
      }}
      onDoubleClick={() => d.onEdit(d.id)}
    >
      <Handle type="target" position={Position.Left} style={{ background: color, border: "2px solid #fff", width: 10, height: 10 }} />
      <div style={{ padding: "10px 14px", display: "flex", gap: 10, alignItems: "center" }}>
        <span style={{ fontSize: 20, lineHeight: 1, color }}>{ICON[d.type] ?? "●"}</span>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 11, fontWeight: 600, letterSpacing: "0.06em", textTransform: "uppercase", color, marginBottom: 2 }}>{d.type}</div>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--fg, #e8eaf6)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{d.label}</div>
          {d.config?.description && (
            <div style={{ fontSize: 11, color: "var(--fg-muted, #7b82a8)", marginTop: 2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{d.config.description}</div>
          )}
        </div>
      </div>
      <Handle type="source" position={Position.Right} style={{ background: color, border: "2px solid #fff", width: 10, height: 10 }} />
    </div>
  );
}

const NODE_COMPONENTS = { flowNode: FlowNodeCard };

// ─── config panel ─────────────────────────────────────────────────────────────
function ConfigPanel({ node, onSave, onDelete, onClose }: {
  node: Node;
  onSave: (id: string, label: string, config: Record<string, string>) => void;
  onDelete: (id: string) => void;
  onClose: () => void;
}) {
  const d = node.data as { label: string; type: string; config: Record<string, string> };
  const [label, setLabel] = useState(d.label);
  const [desc, setDesc]   = useState(d.config?.description ?? "");
  const color = COLOR[d.type] ?? "#6c73ff";

  return (
    <div style={{
      position: "absolute", right: 12, top: 56, width: 280, zIndex: 10,
      background: "var(--surface, #1a1d27)",
      border: "1px solid var(--border, #2d3148)",
      borderRadius: 10, padding: 16,
      boxShadow: "0 8px 32px #0008",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 14 }}>
        <span style={{ color, fontSize: 18 }}>{ICON[d.type]}</span>
        <span style={{ fontWeight: 700, fontSize: 14, color: "var(--fg, #e8eaf6)", flex: 1 }}>Configure {d.type}</span>
        <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--fg-muted, #7b82a8)", fontSize: 16 }}>✕</button>
      </div>

      <label style={labelStyle}>Label</label>
      <input style={inputStyle} value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Step name…" />

      <label style={labelStyle}>Description <span style={{ opacity: 0.5 }}>(what this step does)</span></label>
      <textarea style={{ ...inputStyle, height: 72, resize: "vertical" }} value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="Describe what this step should do…" />

      <div style={{ display: "flex", gap: 8, marginTop: 14 }}>
        <button style={btnDanger} onClick={() => { onDelete(node.id); onClose(); }}>Delete</button>
        <span style={{ flex: 1 }} />
        <button style={btnPrimary(color)} onClick={() => { onSave(node.id, label, { ...d.config, description: desc }); onClose(); }}>Save</button>
      </div>
    </div>
  );
}

const labelStyle: React.CSSProperties = { display: "block", fontSize: 11, fontWeight: 600, letterSpacing: "0.06em", textTransform: "uppercase", color: "var(--fg-muted, #7b82a8)", marginBottom: 4, marginTop: 10 };
const inputStyle: React.CSSProperties = { width: "100%", background: "var(--bg, #0f1117)", border: "1px solid var(--border, #2d3148)", borderRadius: 6, padding: "7px 10px", color: "var(--fg, #e8eaf6)", fontSize: 13, outline: "none", boxSizing: "border-box" };
const btnPrimary = (color: string): React.CSSProperties => ({ background: color, color: "#fff", border: "none", borderRadius: 6, padding: "7px 16px", fontSize: 13, fontWeight: 600, cursor: "pointer" });
const btnDanger: React.CSSProperties = { background: "rgba(248,113,113,0.12)", color: "#f87171", border: "none", borderRadius: 6, padding: "7px 12px", fontSize: 13, cursor: "pointer" };

// ─── main canvas page ─────────────────────────────────────────────────────────
let nodeCounter = 1;

function CanvasPage() {
  const router = useRouter();
  const params = useSearchParams();
  const agentId = params.get("id");

  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [editingNode, setEditingNode] = useState<Node | null>(null);
  const [building, setBuilding]       = useState(false);
  const [error, setError]             = useState("");
  const [agentName, setAgentName]     = useState("My agent");

  const onConnect = useCallback((conn: Connection) => setEdges((eds) => addEdge({ ...conn, animated: true, style: { stroke: "#6c73ff", strokeWidth: 2 } }, eds)), [setEdges]);

  function makeNode(type: string, x: number, y: number): Node {
    const id = `n${nodeCounter++}`;
    const meta = NODE_TYPES_META.find((m) => m.type === type)!;
    return {
      id,
      type: "flowNode",
      position: { x, y },
      data: { id, type, label: meta.label, config: {}, onEdit: (nid: string) => setEditingNode(nodes.find((n) => n.id === nid) ?? null) },
    };
  }

  function addNode(type: string) {
    const x = 80 + (nodes.length % 4) * 240;
    const y = 80 + Math.floor(nodes.length / 4) * 140;
    const n = makeNode(type, x, y);
    setNodes((ns) => [...ns, n]);
  }

  function saveNodeConfig(id: string, label: string, config: Record<string, string>) {
    setNodes((ns) => ns.map((n) => n.id !== id ? n : { ...n, data: { ...n.data, label, config, onEdit: (nid: string) => setEditingNode(ns.find((x) => x.id === nid) ?? null) } }));
  }

  function deleteNode(id: string) {
    setNodes((ns) => ns.filter((n) => n.id !== id));
    setEdges((es) => es.filter((e) => e.source !== id && e.target !== id));
  }

  async function buildAgent() {
    if (nodes.length === 0) { setError("Add at least one node to the canvas."); return; }
    setBuilding(true); setError("");
    const schema: FlowSchema = {
      nodes: nodes.map((n) => {
        const d = n.data as { type: string; label: string; config: Record<string, string> };
        return { id: n.id, type: d.type, label: d.label, config: d.config };
      }),
      edges: edges.map((e) => ({ from: e.source, to: e.target, condition: (e.data as any)?.condition })),
    };
    try {
      const body: Record<string, unknown> = { flow: schema, name: agentName };
      if (agentId) body.agent_id = agentId;
      const { id } = await api<{ id: string }>("/api/agents/from-flow", { body });
      router.push(`/agent/?id=${id}`);
    } catch (e: any) {
      setError(e.message);
      setBuilding(false);
    }
  }

  // keep onEdit callbacks fresh when nodes change
  const nodesWithCallbacks = nodes.map((n) => ({
    ...n,
    data: { ...n.data, onEdit: (nid: string) => setEditingNode(nodes.find((x) => x.id === nid) ?? null) },
  }));

  const editingNodeFresh = editingNode ? nodesWithCallbacks.find((n) => n.id === editingNode.id) ?? null : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100dvh", background: "var(--bg, #0f1117)" }}>
      <TopBar>
        <input
          value={agentName}
          onChange={(e) => setAgentName(e.target.value)}
          style={{ background: "transparent", border: "none", outline: "none", color: "var(--fg, #e8eaf6)", fontSize: 15, fontWeight: 600, marginLeft: 8, width: 220 }}
          placeholder="Agent name…"
        />
        <span style={{ flex: 1 }} />
        {error && <span style={{ color: "#f87171", fontSize: 13, marginRight: 12 }}>{error}</span>}
        <button
          className="btn"
          disabled={building || nodes.length === 0}
          onClick={buildAgent}
          style={{ background: "#6c73ff", color: "#fff", border: "none", borderRadius: 7, padding: "7px 18px", fontWeight: 600, fontSize: 13, cursor: "pointer", opacity: building ? 0.6 : 1 }}
        >
          {building ? "Building…" : "Build agent →"}
        </button>
      </TopBar>

      <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>
        {/* palette */}
        <aside style={{ width: 160, background: "var(--surface, #1a1d27)", borderRight: "1px solid var(--border, #2d3148)", padding: "12px 10px", display: "flex", flexDirection: "column", gap: 6, overflowY: "auto" }}>
          <div style={{ fontSize: 10, fontWeight: 700, letterSpacing: "0.1em", textTransform: "uppercase", color: "var(--fg-muted, #7b82a8)", marginBottom: 4 }}>Add node</div>
          {NODE_TYPES_META.map((m) => (
            <button
              key={m.type}
              onClick={() => addNode(m.type)}
              title={m.desc}
              style={{
                display: "flex", alignItems: "center", gap: 8,
                background: "transparent", border: `1px solid ${m.color}44`,
                borderRadius: 7, padding: "7px 10px", cursor: "pointer",
                color: "var(--fg, #e8eaf6)", fontSize: 12, fontWeight: 500,
                transition: "background 0.1s",
              }}
              onMouseEnter={(e) => (e.currentTarget.style.background = `${m.color}18`)}
              onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
            >
              <span style={{ color: m.color, fontSize: 16, lineHeight: 1 }}>{m.icon}</span>
              {m.label}
            </button>
          ))}
          <div style={{ marginTop: "auto", fontSize: 11, color: "var(--fg-muted, #7b82a8)", lineHeight: 1.5 }}>
            Double-click a node to configure it. Drag between handles to connect.
          </div>
        </aside>

        {/* canvas */}
        <div style={{ flex: 1, position: "relative" }}>
          <ReactFlow
            nodes={nodesWithCallbacks}
            edges={edges}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            nodeTypes={NODE_COMPONENTS}
            fitView
            defaultEdgeOptions={{ animated: true, style: { stroke: "#6c73ff55", strokeWidth: 2 } }}
          >
            <Background color="#2d3148" gap={24} />
            <Controls style={{ background: "var(--surface, #1a1d27)", border: "1px solid var(--border, #2d3148)", borderRadius: 8 }} />
            <MiniMap style={{ background: "var(--surface, #1a1d27)", border: "1px solid var(--border, #2d3148)" }} nodeColor={(n) => COLOR[(n.data as any).type] ?? "#6c73ff"} />
          </ReactFlow>

          {nodes.length === 0 && (
            <div style={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", pointerEvents: "none", gap: 10 }}>
              <div style={{ fontSize: 40, opacity: 0.15 }}>⬡</div>
              <div style={{ fontSize: 15, fontWeight: 600, color: "var(--fg-muted, #7b82a8)", opacity: 0.5 }}>Add nodes from the left panel to design your flow</div>
            </div>
          )}

          {editingNodeFresh && (
            <ConfigPanel
              node={editingNodeFresh}
              onSave={saveNodeConfig}
              onDelete={deleteNode}
              onClose={() => setEditingNode(null)}
            />
          )}
        </div>
      </div>
    </div>
  );
}

export default function CanvasPageWrapper() {
  return (
    <Suspense fallback={<TopBar />}>
      <CanvasPage />
    </Suspense>
  );
}
