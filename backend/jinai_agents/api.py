"""HTTP API for the web app, webhooks, and AI calls from running agents."""
from __future__ import annotations

import hmac
import logging
import time
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select

from . import builder, config, db, llm, runner, scheduler
from .db import Agent, Event, Message, Run, Usage, Version, now, session
from .store import (REAL_KINDS, StoreError, add_message, deploy, health, iso, run_brief, set_secret,
                    steps_from_code, syntax_errors, validate_files)

log = logging.getLogger("jinai.api")


# --------------------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    with session() as s:  # recover from a restart
        for r in s.scalars(select(Run).where(Run.status.in_(("queued", "running")))):
            r.status, r.error, r.finished_at = "failed", "Interrupted: the server restarted during this run", now()
        for a in s.scalars(select(Agent).where(Agent.builder_busy.is_(True))):
            a.builder_busy, a.builder_activity = False, ""
        for a in s.scalars(select(Agent).where(Agent.status == "live")):
            scheduler.schedule_next(a)
    if config.settings.scheduler_enabled:
        scheduler.start()
    if not config.settings.admin_token:
        log.warning("JINAI_ADMIN_TOKEN is not set: the API is open. Only do this on your own machine.")
    yield
    scheduler.stop()


app = FastAPI(title="Jinai Agents", version="0.1.0", lifespan=lifespan)


@app.exception_handler(StoreError)
async def _store_error(_, exc: StoreError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def auth(authorization: str = Header(default="")):
    token = config.settings.admin_token
    if not token:
        return
    given = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given.encode(), token.encode()):
        raise HTTPException(401, "Sign in required")


def get_agent(s, agent_id: str) -> Agent:
    a = s.get(Agent, agent_id)
    if a is None:
        raise HTTPException(404, "Agent not found")
    return a


def trigger_text(a: Agent) -> str:
    if a.trigger_type == "schedule":
        return describe_cron(a.schedule) + (f" ({a.timezone})" if a.timezone != "UTC" else " UTC")
    return {"webhook": "Webhook", "manual": "Manual"}.get(a.trigger_type, a.trigger_type)


def describe_cron(c: str) -> str:
    for label, expr in scheduler.PRESETS.items():
        if expr == c:
            return label.capitalize()
    parts = c.split()
    if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit() and parts[2:] == ["*", "*", "*"]:
        return f"Daily {int(parts[1]):02d}:{int(parts[0]):02d}"
    days = {"1": "Mon", "2": "Tue", "3": "Wed", "4": "Thu", "5": "Fri", "6": "Sat", "0": "Sun", "1-5": "Weekdays"}
    if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit() and parts[2:4] == ["*", "*"] and parts[4] in days:
        return f"{days[parts[4]]} {int(parts[1]):02d}:{int(parts[0]):02d}"
    return f"Cron {c}"


def webhook_url(a: Agent) -> str:
    return f"{config.settings.public_url}/hooks/{a.id}/{a.webhook_secret}"


def agent_row(s, a: Agent) -> dict:
    return {"id": a.id, "name": a.name, "status": a.status, "trigger": trigger_text(a),
            "trigger_type": a.trigger_type, "next_run_at": iso(a.next_run_at), "builder_busy": a.builder_busy,
            "health": health(s, a), "draft_dirty": a.draft_dirty, "updated_at": iso(a.updated_at)}


# --------------------------------------------------------------------------- overview
@app.get("/api/me", dependencies=[Depends(auth)])
def me():
    return {"ok": True, "ai_configured": bool(config.settings.anthropic_api_key) or llm._llm is not None,
            "runner": config.settings.runner, "email_configured": bool(config.settings.smtp_host),
            "presets": scheduler.PRESETS}


@app.get("/api/summary", dependencies=[Depends(auth)])
def summary():
    with session() as s:
        agents = s.scalars(select(Agent)).all()
        rows = [agent_row(s, a) for a in agents]
        week = now() - timedelta(days=7)
        month_start = now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        fixed = s.scalar(select(func.count()).select_from(Event).where(Event.kind == "fixed", Event.created_at >= week)) or 0
        cost = s.scalar(select(func.coalesce(func.sum(Usage.cost), 0)).where(Usage.created_at >= month_start)) or 0
        runs_today = s.scalar(select(func.count()).select_from(Run).where(
            Run.kind.in_(REAL_KINDS), Run.created_at >= now() - timedelta(days=1))) or 0
    working = sum(1 for r in rows if r["status"] == "live" and (not r["health"]["last_run"] or
                                                                r["health"]["last_run"]["status"] in ("success", "running", "queued")))
    return {"agents": len(rows), "working": working, "fixed_7d": fixed,
            "needs_you": sum(1 for r in rows if r["status"] == "needs_attention"),
            "paused": sum(1 for r in rows if r["status"] == "paused"),
            "drafts": sum(1 for r in rows if r["status"] == "draft"),
            "ai_cost_month": round(float(cost), 2), "runs_24h": runs_today}


@app.get("/api/agents", dependencies=[Depends(auth)])
def list_agents():
    with session() as s:
        agents = s.scalars(select(Agent).order_by(Agent.created_at.desc())).all()
        order = {"needs_attention": 0, "live": 1, "draft": 2, "paused": 3}
        rows = [agent_row(s, a) for a in agents]
    return sorted(rows, key=lambda r: order.get(r["status"], 9))


@app.get("/api/events", dependencies=[Depends(auth)])
def events(limit: int = 20):
    with session() as s:
        ev = s.scalars(select(Event).order_by(Event.id.desc()).limit(min(limit, 100))).all()
        names = {a.id: a.name for a in s.scalars(select(Agent))}
        return [{"id": e.id, "agent_id": e.agent_id, "agent": names.get(e.agent_id, "?"), "kind": e.kind,
                 "text": e.text, "run_id": e.run_id, "created_at": iso(e.created_at)} for e in ev]


# --------------------------------------------------------------------------- agents
class NewAgent(BaseModel):
    request: str


@app.post("/api/agents", dependencies=[Depends(auth)])
def create_agent(body: NewAgent):
    text = body.request.strip()
    if len(text) < 5:
        raise HTTPException(400, "Describe what the agent should do")
    with session() as s:
        a = Agent(name=_guess_name(text), description=text[:2000],
                  alert_email=config.settings.owner_email)
        s.add(a)
        s.flush()
        agent_id = a.id
    builder.handle_message(agent_id, text)
    return {"id": agent_id}


def _guess_name(text: str) -> str:
    words = text.strip().rstrip(".").split()
    return (" ".join(words[:6]) + ("…" if len(words) > 6 else "")).capitalize()[:80]


@app.get("/api/agents/{agent_id}", dependencies=[Depends(auth)])
def get_agent_detail(agent_id: str):
    with session() as s:
        a = get_agent(s, agent_id)
        live = s.get(Version, a.live_version_id) if a.live_version_id else None
        versions = s.scalars(select(Version).where(Version.agent_id == agent_id).order_by(Version.number.desc()).limit(30)).all()
        files = a.draft_files or {}
        return {
            **agent_row(s, a), "description": a.description, "schedule": a.schedule, "timezone": a.timezone,
            "webhook_url": webhook_url(a), "alert_email": a.alert_email, "max_steps": a.max_steps,
            "max_cost_per_run": a.max_cost_per_run, "timeout_s": a.timeout_s, "auto_repair": a.auto_repair,
            "needed_secrets": a.needed_secrets, "secrets_set": sorted((a.secrets_enc or {}).keys()),
            "memory": a.state, "builder_activity": a.builder_activity,
            "files": files, "outline": steps_from_code(files.get("agent.py", "")) if files.get("agent.py") else [],
            "syntax_errors": syntax_errors(files),
            "live_version": live.number if live else None,
            "versions": [{"number": v.number, "note": v.note, "by": v.created_by, "created_at": iso(v.created_at),
                          "live": v.id == a.live_version_id} for v in versions],
        }


@app.delete("/api/agents/{agent_id}", dependencies=[Depends(auth)])
def delete_agent(agent_id: str):
    with session() as s:
        s.delete(get_agent(s, agent_id))
    return {"ok": True}


@app.get("/api/agents/{agent_id}/messages", dependencies=[Depends(auth)])
def messages(agent_id: str, after: int = 0):
    with session() as s:
        get_agent(s, agent_id)
        ms = s.scalars(select(Message).where(Message.agent_id == agent_id, Message.id > after)
                       .order_by(Message.id).limit(500)).all()
        return [{"id": m.id, "role": m.role, "text": m.text, "data": m.data, "created_at": iso(m.created_at)} for m in ms]


class Chat(BaseModel):
    text: str


@app.post("/api/agents/{agent_id}/messages", dependencies=[Depends(auth)])
def post_message(agent_id: str, body: Chat):
    if not body.text.strip():
        raise HTTPException(400, "Empty message")
    with session() as s:
        get_agent(s, agent_id)
    try:
        builder.handle_message(agent_id, body.text.strip())
    except StoreError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


class Files(BaseModel):
    files: dict[str, str]


@app.put("/api/agents/{agent_id}/files", dependencies=[Depends(auth)])
def save_files(agent_id: str, body: Files):
    files = validate_files(body.files)
    with session() as s:
        a = get_agent(s, agent_id)
        if a.builder_busy:
            raise HTTPException(409, "The builder is editing this agent; wait for it to finish")
        if files != (a.draft_files or {}):
            a.draft_files = files
            a.draft_dirty = True
            add_message(s, agent_id, "activity", "You edited the code")
    return {"outline": steps_from_code(files.get("agent.py", "")), "syntax_errors": syntax_errors(files)}


class TestBody(BaseModel):
    event: dict | None = None


@app.post("/api/agents/{agent_id}/test", dependencies=[Depends(auth)])
def test(agent_id: str, body: TestBody):
    with session() as s:
        a = get_agent(s, agent_id)
        files = dict(a.draft_files or {})
    if "agent.py" not in files:
        raise HTTPException(400, "There's no agent.py yet")
    errs = syntax_errors(files)
    if errs:
        raise HTTPException(400, f"Syntax error in {errs[0]['file']} line {errs[0]['line']}: {errs[0]['message']}")
    return runner.test_run(agent_id, files, body.event)


class DeployBody(BaseModel):
    note: str = ""


@app.post("/api/agents/{agent_id}/deploy", dependencies=[Depends(auth)])
def deploy_draft(agent_id: str, body: DeployBody):
    with session() as s:
        a = get_agent(s, agent_id)
        v = deploy(s, a, dict(a.draft_files or {}), body.note, "user")
        a.draft_dirty = False
        add_message(s, agent_id, "activity", f"Deployed v{v.number}")
        return {"version": v.number, "next_run_at": iso(a.next_run_at)}


class Rollback(BaseModel):
    number: int


@app.get("/api/agents/{agent_id}/versions/{number}", dependencies=[Depends(auth)])
def get_version(agent_id: str, number: int):
    with session() as s:
        v = s.scalars(select(Version).where(Version.agent_id == agent_id, Version.number == number)).first()
        if v is None:
            raise HTTPException(404, "Version not found")
        return {"number": v.number, "files": v.files, "note": v.note, "by": v.created_by, "created_at": iso(v.created_at)}


@app.post("/api/agents/{agent_id}/rollback", dependencies=[Depends(auth)])
def rollback(agent_id: str, body: Rollback):
    with session() as s:
        a = get_agent(s, agent_id)
        old = s.scalars(select(Version).where(Version.agent_id == agent_id, Version.number == body.number)).first()
        if old is None:
            raise HTTPException(404, "Version not found")
        v = deploy(s, a, dict(old.files), f"Rolled back to v{old.number}", "user")
        a.draft_files, a.draft_dirty = dict(old.files), False
        add_message(s, agent_id, "activity", f"Rolled back: v{v.number} = v{old.number}")
        return {"version": v.number}


class SettingsBody(BaseModel):
    name: str | None = None
    trigger_type: str | None = None
    schedule: str | None = None
    timezone: str | None = None
    alert_email: str | None = None
    max_steps: int | None = None
    max_cost_per_run: float | None = None
    timeout_s: int | None = None
    auto_repair: bool | None = None


@app.put("/api/agents/{agent_id}/settings", dependencies=[Depends(auth)])
def update_settings(agent_id: str, body: SettingsBody):
    data = body.model_dump(exclude_none=True)
    with session() as s:
        a = get_agent(s, agent_id)
        if "auto_repair" in data:
            a.auto_repair = data.pop("auto_repair")
    if data:
        try:
            builder._update_settings(agent_id, data)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e))
    return get_agent_detail(agent_id)


class SecretBody(BaseModel):
    name: str
    value: str | None = None


@app.put("/api/agents/{agent_id}/secrets", dependencies=[Depends(auth)])
def put_secret(agent_id: str, body: SecretBody):
    with session() as s:
        a = get_agent(s, agent_id)
        set_secret(a, body.name, body.value)
        return {"secrets_set": sorted((a.secrets_enc or {}).keys())}


@app.delete("/api/agents/{agent_id}/memory", dependencies=[Depends(auth)])
def clear_memory(agent_id: str):
    with session() as s:
        get_agent(s, agent_id).state = {}
    return {"ok": True}


class RunBody(BaseModel):
    event: dict | None = None


@app.post("/api/agents/{agent_id}/run", dependencies=[Depends(auth)])
def run_now(agent_id: str, body: RunBody):
    try:
        return {"run_id": runner.queue_run(agent_id, "manual", body.event)}
    except runner.RunnerError as e:
        raise HTTPException(400, str(e))


@app.post("/api/agents/{agent_id}/pause", dependencies=[Depends(auth)])
def pause(agent_id: str):
    with session() as s:
        a = get_agent(s, agent_id)
        if a.status in ("live", "needs_attention"):
            a.status, a.next_run_at = "paused", None
            s.add(Event(agent_id=a.id, kind="paused", text="Paused by you"))
    return {"ok": True}


@app.post("/api/agents/{agent_id}/resume", dependencies=[Depends(auth)])
def resume(agent_id: str):
    with session() as s:
        a = get_agent(s, agent_id)
        if not a.live_version_id:
            raise HTTPException(400, "Deploy the agent first")
        a.status, a.consecutive_failures = "live", 0
        scheduler.schedule_next(a)
        s.add(Event(agent_id=a.id, kind="resumed", text="Resumed"))
    return {"ok": True}


@app.get("/api/agents/{agent_id}/runs", dependencies=[Depends(auth)])
def list_runs(agent_id: str, limit: int = 30, include_tests: bool = True):
    with session() as s:
        get_agent(s, agent_id)
        q = select(Run).where(Run.agent_id == agent_id)
        if not include_tests:
            q = q.where(Run.kind.in_(REAL_KINDS))
        rs = s.scalars(q.order_by(Run.created_at.desc()).limit(min(limit, 200))).all()
        return [run_brief(r) for r in rs]


@app.get("/api/runs/{run_id}", dependencies=[Depends(auth)])
def get_run(run_id: str):
    with session() as s:
        r = s.get(Run, run_id)
        if r is None:
            raise HTTPException(404, "Run not found")
        a = s.get(Agent, r.agent_id)
        return {**run_brief(r), "agent_id": r.agent_id, "agent_name": a.name, "input": r.input, "output": r.output,
                "steps": r.steps, "checks": r.checks, "notifications": r.notifications, "logs": r.logs,
                "error": r.error, "ai_calls": r.ai_calls, "started_at": iso(r.started_at)}


# --------------------------------------------------------------------------- webhooks (no sign-in; secret in URL)
@app.post("/hooks/{agent_id}/{secret}")
async def webhook(agent_id: str, secret: str, request: Request, wait: int = 0):
    with session() as s:
        a = s.get(Agent, agent_id)
        if a is None or not hmac.compare_digest(secret.encode(), a.webhook_secret.encode()):
            raise HTTPException(404, "Not found")
        if a.trigger_type != "webhook":
            raise HTTPException(409, "This agent is not triggered by webhooks")
        if a.status != "live":
            raise HTTPException(409, f"Agent is {a.status}")
    try:
        body = await request.json()
    except ValueError:
        body = {"text": (await request.body()).decode("utf-8", "replace")[:100_000]}
    if not isinstance(body, dict):
        body = {"data": body}
    try:
        run_id = runner.queue_run(agent_id, "webhook", body)
    except runner.RunnerError as e:
        raise HTTPException(409, str(e))
    if not wait:
        return JSONResponse({"run_id": run_id, "status": "queued"}, status_code=202)
    import asyncio
    deadline = time.time() + min(wait, 120)
    while time.time() < deadline:
        await asyncio.sleep(0.5)
        with session() as s:
            r = s.get(Run, run_id)
            if r.status not in ("queued", "running"):
                return {"run_id": run_id, "status": r.status, "output": r.output}
    return JSONResponse({"run_id": run_id, "status": "running"}, status_code=202)


# --------------------------------------------------------------------------- AI for running agents
class LLMBody(BaseModel):
    system: str = ""
    messages: list
    tools: list | None = None
    max_tokens: int = 1500


@app.post("/internal/llm")
def internal_llm(body: LLMBody, authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    with session() as s:
        r = s.scalars(select(Run).where(Run.token == token, Run.status == "running")).first() if token else None
        if r is None:
            raise HTTPException(401, "Invalid run token")
        a = s.get(Agent, r.agent_id)
        if r.ai_cost >= a.max_cost_per_run:
            raise HTTPException(402, f"AI budget for this run (${a.max_cost_per_run:.2f}) is used up")
        if r.ai_calls >= max(a.max_steps * 3, 30):
            raise HTTPException(402, "Too many AI calls in this run")
        run_id, agent_id = r.id, r.agent_id
    try:
        resp = llm.get_llm().create(model=config.settings.agent_model, system=body.system, messages=body.messages,
                                    tools=body.tools, max_tokens=min(body.max_tokens, 8000))
    except llm.LLMError as e:
        raise HTTPException(502, str(e))
    usage = resp.get("usage", {})
    cost = llm.cost_of(usage)
    with session() as s:
        r = s.get(Run, run_id)
        r.ai_calls += 1
        r.ai_cost += cost
        from .store import add_usage
        add_usage(s, agent_id, "run", usage, cost)
    return {"text": llm.text_of(resp), "content": resp.get("content", []), "stop_reason": resp.get("stop_reason"),
            "cost": round(cost, 6)}


# --------------------------------------------------------------------------- web app (static export of web/)
@app.get("/healthz")
def healthz():
    return {"ok": True}


def mount_web():
    web = config.settings.web_dir
    if web.exists():
        app.mount("/", StaticFiles(directory=str(web), html=True), name="web")
    else:
        @app.get("/")
        def _no_web():
            return {"detail": "Web app not built. Run `npm run build` in web/."}


mount_web()
