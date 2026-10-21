"""Agent helpers: versions, deploy, secrets, steps view, health."""
from __future__ import annotations

import ast
import base64
import hashlib
import posixpath
from datetime import datetime, timedelta

from sqlalchemy import func, select

from . import config
from .db import Agent, Event, Message, Run, Usage, Version, as_utc, now

MAX_FILE = 200_000
MAX_FILES = 40
ALLOWED_EXT = {".py", ".md", ".txt", ".json", ".csv", ".yaml", ".yml"}


class StoreError(Exception):
    pass


# --------------------------------------------------------------------------- files
def clean_path(path: str) -> str:
    p = posixpath.normpath((path or "").strip().lstrip("/"))
    if p in ("", ".") or p.startswith("..") or "\\" in p or "\x00" in p:
        raise StoreError(f"invalid path: {path!r}")
    if posixpath.splitext(p)[1].lower() not in ALLOWED_EXT:
        raise StoreError(f"file type not allowed: {path} (use {', '.join(sorted(ALLOWED_EXT))})")
    if p.split("/")[0] in ("io", "__pycache__") or p.startswith("."):
        raise StoreError(f"reserved path: {path}")
    return p


def validate_files(files: dict) -> dict:
    if len(files) > MAX_FILES:
        raise StoreError(f"too many files (max {MAX_FILES})")
    out = {}
    for k, v in files.items():
        if not isinstance(v, str):
            raise StoreError(f"{k}: content must be text")
        if len(v) > MAX_FILE:
            raise StoreError(f"{k}: file too large")
        out[clean_path(k)] = v
    return out


def syntax_errors(files: dict) -> list[dict]:
    errs = []
    for path, src in files.items():
        if path.endswith(".py"):
            try:
                ast.parse(src, filename=path)
            except SyntaxError as e:
                errs.append({"file": path, "line": e.lineno, "message": e.msg})
    return errs


# --------------------------------------------------------------------------- steps view
_AI_FUNCS = {"ai", "agent"}


def steps_from_code(src: str) -> list[dict]:
    """Plain-English outline of agent.py: every step("...") in source order, nested, with flags for AI
    use, checks and notifications inside it."""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [{"name": f"Syntax error on line {e.lineno}: {e.msg}", "line": e.lineno or 1, "depth": 0,
                 "ai": False, "checks": 0, "notifies": False, "error": True}]

    def call_name(c):
        f = c.func
        return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None

    def step_label(c):
        if isinstance(c, ast.Call) and call_name(c) == "step" and c.args:
            a = c.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                return a.value
            if isinstance(a, ast.JoinedStr):
                return "".join(v.value if isinstance(v, ast.Constant) else "…" for v in a.values)
            return "(step)"
        return None

    out: list[dict] = []

    def flags(nodes):
        ai = notifies = False
        checks = 0
        for n in nodes:
            for sub in ast.walk(n):
                if isinstance(sub, ast.Call):
                    nm = call_name(sub)
                    ai |= nm in _AI_FUNCS
                    notifies |= nm == "notify"
                    checks += nm == "check"
        return ai, checks, notifies

    def visit(body, depth):
        for node in body:
            label = None
            inner = None
            if isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    label = step_label(item.context_expr) or label
                inner = node.body
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.decorator_list:
                    label = step_label(d) or label
                inner = node.body
            if label is not None:
                ai, checks, notifies = flags(inner)
                out.append({"name": label, "line": node.lineno, "depth": depth, "ai": ai, "checks": checks,
                            "notifies": notifies})
                visit(inner, depth + 1)
            else:
                for field in ("body", "orelse", "finalbody", "handlers"):
                    sub = getattr(node, field, None)
                    if isinstance(sub, list):
                        visit([s for s in sub if isinstance(s, ast.AST)], depth)
                if isinstance(node, ast.ExceptHandler):
                    visit(node.body, depth)

    visit(tree.body, 0)
    return out


# --------------------------------------------------------------------------- secrets (encrypted at rest)
def _fernet():
    from cryptography.fernet import Fernet
    key = config.settings.secret_key
    if not key:
        # derived fallback so local dev works; production must set JINAI_SECRET_KEY
        key = base64.urlsafe_b64encode(hashlib.sha256(b"jinai-dev-key:" + config.settings.admin_token.encode()).digest())
    return Fernet(key)


def set_secret(agent: Agent, name: str, value: str | None):
    name = name.strip().upper()
    if not name.replace("_", "").isalnum() or not name[0].isalpha():
        raise StoreError("secret names use letters, digits and _ (e.g. SLACK_TOKEN)")
    d = dict(agent.secrets_enc or {})
    if value is None or value == "":
        d.pop(name, None)
    else:
        d[name] = _fernet().encrypt(value.encode()).decode()
    agent.secrets_enc = d


def secret_env(agent: Agent) -> dict:
    f = _fernet()
    out = {}
    for k, v in (agent.secrets_enc or {}).items():
        try:
            out["JINAI_SECRET_" + k] = f.decrypt(v.encode()).decode()
        except Exception:  # key rotated: skip
            continue
    return out


# --------------------------------------------------------------------------- versions
def live_version(s, agent: Agent) -> Version | None:
    return s.get(Version, agent.live_version_id) if agent.live_version_id else None


def deploy(s, agent: Agent, files: dict, note: str, by: str) -> Version:
    files = validate_files(files)
    errs = syntax_errors(files)
    if errs:
        e = errs[0]
        raise StoreError(f"syntax error in {e['file']} line {e['line']}: {e['message']}")
    if "agent.py" not in files:
        raise StoreError("agent.py is required")
    n = (s.scalar(select(func.max(Version.number)).where(Version.agent_id == agent.id)) or 0) + 1
    v = Version(agent_id=agent.id, number=n, files=files, note=note[:300], created_by=by)
    s.add(v)
    s.flush()
    agent.live_version_id = v.id
    if agent.status in ("draft", "needs_attention"):
        agent.status = "live"
    agent.consecutive_failures = 0
    s.add(Event(agent_id=agent.id, kind="deployed" if by != "repair" else "fixed",
                text=f"v{n} deployed" + (f": {note}" if note else "")))
    from .scheduler import schedule_next
    schedule_next(agent)
    return v


def add_message(s, agent_id: str, role: str, text: str, data: dict | None = None):
    s.add(Message(agent_id=agent_id, role=role, text=text, data=data or {}))


def add_usage(s, agent_id: str, kind: str, usage: dict, cost: float):
    s.add(Usage(agent_id=agent_id, kind=kind, input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0), cost=cost))


# --------------------------------------------------------------------------- health
REAL_KINDS = ("schedule", "manual", "webhook")
DONE = ("success", "failed", "suspicious")


def health(s, agent: Agent) -> dict:
    since = now() - timedelta(days=30)
    rows = s.execute(select(Run.status, func.count()).where(
        Run.agent_id == agent.id, Run.kind.in_(REAL_KINDS), Run.status.in_(DONE), Run.created_at >= since
    ).group_by(Run.status)).all()
    counts = {k: v for k, v in rows}
    total = sum(counts.values())
    fixes = s.scalar(select(func.count()).select_from(Event).where(
        Event.agent_id == agent.id, Event.kind == "fixed", Event.created_at >= since)) or 0
    recent = s.scalars(select(Run).where(Run.agent_id == agent.id, Run.kind.in_(REAL_KINDS))
                       .order_by(Run.created_at.desc()).limit(30)).all()[::-1]
    return {"runs_30d": total, "success_30d": counts.get("success", 0),
            "recent": [{"id": r.id, "status": r.status, "repair": r.repair_status,
                        "at": iso(r.created_at)} for r in recent],
            "success_rate": round(counts.get("success", 0) / total, 3) if total else None,
            "fixed_30d": fixes,
            "last_run": run_brief(recent[-1]) if recent else None}


def run_brief(r: Run) -> dict:
    return {"id": r.id, "kind": r.kind, "status": r.status, "version": r.version_number,
            "created_at": iso(r.created_at), "finished_at": iso(r.finished_at),
            "duration_s": round((as_utc(r.finished_at) - as_utc(r.started_at)).total_seconds(), 1)
            if r.finished_at and r.started_at else None,
            "summary": run_summary(r), "ai_cost": round(r.ai_cost, 4), "repair_status": r.repair_status}


def run_summary(r: Run) -> str:
    if r.status in ("queued", "running"):
        return r.status
    if r.status == "failed":
        return (r.error.strip().splitlines() or ["failed"])[-1][:160]
    bad = [c for c in r.checks if not c.get("ok")]
    if bad:
        return "check failed: " + bad[0]["message"][:140]
    notes = [n for st in r.steps for n in st.get("notes", [])]
    sent = len(r.notifications)
    parts = []
    if notes:
        parts.append(notes[-1][:100])
    if sent:
        parts.append(f"{sent} message{'s' if sent > 1 else ''} sent" if r.kind != "test" else f"{sent} message(s) (not sent in tests)")
    return ", ".join(parts) or "ok"


def iso(dt: datetime | None) -> str | None:
    dt = as_utc(dt)
    return dt.isoformat() if dt else None
