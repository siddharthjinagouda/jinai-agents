"""The builder: Claude in a tool loop that writes, tests and fixes agents.

Two modes:
  build  – the user asked for something in chat; works on the agent's draft.
  repair – a live run failed or looked wrong; works on a copy of the live code and redeploys if fixed.
"""
from __future__ import annotations

import json
import logging
import threading
from zoneinfo import ZoneInfo

from sqlalchemy import select

from . import config, llm, runner
from .db import Agent, Message, Run, session
from .scheduler import schedule_next, valid_cron
from .store import (REAL_KINDS, StoreError, add_message, add_usage, clean_path, run_brief, steps_from_code,
                    syntax_errors, validate_files)

log = logging.getLogger("jinai.builder")

LIBRARY_DOC = '''
## The `jinai` library (pre-installed; also available: httpx, beautifulsoup4, Python standard library)

```python
from jinai import step, check, fetch, state, ai, agent, notify, secret, prompt, tools, log

with step("Read pricing page") as s:      # every logical stage MUST be a step; names are plain English
    s.note("3 plans found")               # short result shown in the run history
check(price > 0, "price is a positive number")   # sanity check; failing => run is 'suspicious',
                                          # notifications are held and the repair loop looks at it
page = fetch(url)                         # Page; raises FetchError on HTTP >= 400 (allow_error=True to keep going)
page.text / page.html / page.json() / page.select(css) / page.select_one(css) / page.links / page.meta(name)
page.structured_data()                    # JSON-LD (schema.org Product/Offer...) – prefer this for prices
fetch(url, browser=True)                  # renders JavaScript with headless Chromium (slower; only if needed)
fetch(url, method="POST", json={...}, headers={...})   # APIs
state.get("key", default) / state.set("key", value)   # memory between runs (JSON values); saved only on success
ai("Summarise ...", json=False)           # one Claude call; json=True returns parsed JSON
agent(goal, tools=[fn1, fn2], max_steps=10, context=data)   # AI decides the steps, calling your Python
                                          # functions (type hints + docstring become the tool description)
tools.fetch_page / tools.fetch_links / tools.fetch_json     # ready-made tools for agent()
notify(message, subject=None, to=None)    # email to the owner (sent after the run, only if checks passed)
secret("SLACK_TOKEN")                     # owner-provided secret; raises MissingSecret if not set
prompt("research", company=name)          # loads prompts/research.md, fills {{company}}
log("debug info")                         # goes to the run's logs
```
'''

SYSTEM = f"""You are Jinai's agent builder. You write, test and maintain small Python agents that run
unattended on Jinai's servers. The user describes what they want; you build it as real, readable code.

## How an agent is structured
- `agent.py` must define `run(event)`. `event` is the trigger input: {{}} for scheduled/manual runs, the JSON
  body for webhooks. Return a small JSON-serialisable summary (it is stored as the run's output).
- Put instructions for AI steps in `prompts/<name>.md` (plain English the owner can tweak), loaded with prompt().
- Optional `checks` live inside agent.py as check() calls; optional `README.md` (one paragraph, what it does).
- Keep it simple: one agent.py, usually under 150 lines, no classes unless needed, clear constants at the top
  (URLs, thresholds, recipients) so the owner can edit them.
{LIBRARY_DOC}
## Rules
1. Wrap every logical stage in `with step("...")` – the owner sees these as the agent's plain-English steps.
2. Add check() calls that would catch a silently wrong result (empty values, impossible numbers, missing fields).
3. Prefer plain code; use ai()/agent() only where judgement or language is needed – runs cost money.
4. Prefer stable sources: official APIs, RSS, JSON-LD structured data, then CSS selectors on visible HTML.
5. Never hard-code credentials. Use secret("NAME") and declare it with update_settings(needed_secrets=[...]).
6. First runs: use state to remember previous values; on the first run just store a baseline (don't alert).
7. ALWAYS test_run after writing or changing code. If it fails, read the error, fix, and test again. If a test
   fails only because a secret is missing or the site blocks automated access, stop and explain.
8. Set the trigger with update_settings: schedule (5-field cron + IANA timezone), webhook, or manual.
9. Finish with a short reply for the owner: what the agent does (as numbered steps), its trigger, what the test
   run showed (real values), anything they must do (add a secret), and any limits. No code in the reply.
   Don't say it's deployed – the owner reviews and clicks Deploy.
10. If a request can't work (login with 2FA, CAPTCHA, site forbids bots, needs a service you can't reach),
    say so plainly instead of building something that will fail silently.
"""

REPAIR_SYSTEM = SYSTEM + """
## You are in REPAIR mode
A live run of this agent failed or failed a check. No human is watching. Diagnose from the run details, fix the
code with the smallest sensible change, then test_run until it passes. Don't change what the agent does or who
it notifies. If it can't be fixed by changing code (missing secret, site now blocks bots, page removed, external
service down, the check itself caught a real-world change that the owner should know about), don't change
anything – explain why in 2-3 sentences and what the owner can do. End with one line starting with
"FIXED:" or "NOT FIXED:" followed by a one-sentence reason the owner will read.
"""

TOOLS = [
    {"name": "read_file", "description": "Read a file of the agent.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Create or overwrite a file (agent.py, prompts/x.md, README.md...).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                      "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace an exact, unique piece of text in a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"},
                                                       "new": {"type": "string"}}, "required": ["path", "old", "new"]}},
    {"name": "delete_file", "description": "Delete a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "update_settings", "description": (
        "Change agent settings. trigger_type: schedule|webhook|manual. schedule: 5-field cron. timezone: IANA "
        "name. needed_secrets: names the code reads with secret(). Only pass fields you want to change."),
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "trigger_type": {"type": "string", "enum": ["schedule", "webhook", "manual"]},
         "schedule": {"type": "string"}, "timezone": {"type": "string"}, "alert_email": {"type": "string"},
         "needed_secrets": {"type": "array", "items": {"type": "string"}},
         "max_steps": {"type": "integer"}, "max_cost_per_run": {"type": "number"}, "timeout_s": {"type": "integer"}}}},
    {"name": "test_run", "description": (
        "Run the current code once in the sandbox and get steps, checks, would-be notifications, output and "
        "errors. Nothing is sent and memory is not saved. Pass `event` to simulate a webhook body."),
     "input_schema": {"type": "object", "properties": {"event": {"type": "object"}}}},
    {"name": "list_runs", "description": "Recent real runs of the live agent (status and summary).",
     "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "get_run", "description": "Full detail of one run: steps, checks, error, logs.",
     "input_schema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}},
    {"name": "view_memory", "description": "What the agent currently remembers (state).",
     "input_schema": {"type": "object", "properties": {}}},
]

_locks: dict[str, threading.Lock] = {}


class Workspace:
    """The files the builder edits: the draft (build) or a copy of live code (repair)."""

    def __init__(self, agent_id: str, files: dict, persist: bool):
        self.agent_id, self.files, self.persist = agent_id, dict(files), persist
        self.changed = False
        self.last_test: dict | None = None

    def save(self):
        if self.persist:
            with session() as s:
                a = s.get(Agent, self.agent_id)
                a.draft_files = dict(self.files)
                a.draft_dirty = True


def _activity(agent_id: str, text: str, data: dict | None = None):
    with session() as s:
        add_message(s, agent_id, "activity", text, data)
        a = s.get(Agent, agent_id)
        a.builder_activity = text[:300]


def _context(agent_id: str, ws: Workspace) -> str:
    with session() as s:
        a = s.get(Agent, agent_id)
        recent = s.scalars(select(Run).where(Run.agent_id == agent_id, Run.kind.in_(REAL_KINDS))
                           .order_by(Run.created_at.desc()).limit(5)).all()
        info = {
            "name": a.name, "status": a.status, "trigger_type": a.trigger_type, "schedule": a.schedule,
            "timezone": a.timezone, "alert_email": a.alert_email or "(owner default)",
            "secrets_set": sorted((a.secrets_enc or {}).keys()), "needed_secrets": a.needed_secrets,
            "limits": {"max_steps": a.max_steps, "max_cost_per_run": a.max_cost_per_run, "timeout_s": a.timeout_s},
            "recent_runs": [{k: b[k] for k in ("id", "status", "created_at", "summary")} for b in map(run_brief, recent)],
        }
    total = sum(len(v) for v in ws.files.values())
    if not ws.files:
        files_txt = "(no files yet)"
    elif total < 40_000:
        files_txt = "\n\n".join(f"### {p}\n```\n{c}\n```" for p, c in sorted(ws.files.items()))
    else:
        files_txt = "\n".join(f"- {p} ({len(c)} chars)" for p, c in sorted(ws.files.items()))
    return f"## Agent settings\n```json\n{json.dumps(info, indent=1, default=str)}\n```\n\n## Current files\n{files_txt}"


def _tool(agent_id: str, ws: Workspace, name: str, args: dict) -> tuple[str, bool]:
    try:
        if name == "read_file":
            p = clean_path(args["path"])
            if p not in ws.files:
                return f"{p} does not exist. Files: {sorted(ws.files)}", True
            return ws.files[p], False
        if name == "write_file":
            p = clean_path(args["path"])
            new = p not in ws.files
            ws.files[p] = args["content"]
            validate_files(ws.files)
            ws.changed = True
            ws.save()
            _activity(agent_id, f"{'Created' if new else 'Updated'} {p}")
            errs = syntax_errors({p: ws.files[p]})
            return (f"Saved {p}." + (f" Syntax error line {errs[0]['line']}: {errs[0]['message']}" if errs else "")), False
        if name == "edit_file":
            p = clean_path(args["path"])
            src = ws.files.get(p)
            if src is None:
                return f"{p} does not exist", True
            n = src.count(args["old"])
            if n != 1:
                return f"`old` text found {n} times in {p}; it must match exactly once", True
            ws.files[p] = src.replace(args["old"], args["new"])
            ws.changed = True
            ws.save()
            _activity(agent_id, f"Edited {p}")
            errs = syntax_errors({p: ws.files[p]})
            return (f"Edited {p}." + (f" Syntax error line {errs[0]['line']}: {errs[0]['message']}" if errs else "")), False
        if name == "delete_file":
            p = clean_path(args["path"])
            ws.files.pop(p, None)
            ws.changed = True
            ws.save()
            _activity(agent_id, f"Deleted {p}")
            return f"Deleted {p}", False
        if name == "update_settings":
            return _update_settings(agent_id, args), False
        if name == "test_run":
            if "agent.py" not in ws.files:
                return "Write agent.py first.", True
            errs = syntax_errors(ws.files)
            if errs:
                e = errs[0]
                return f"Syntax error in {e['file']} line {e['line']}: {e['message']}. Fix before testing.", True
            _activity(agent_id, "Running a test…")
            res = runner.test_run(agent_id, ws.files, args.get("event"))
            ws.last_test = res
            ok_checks = sum(c["ok"] for c in res["checks"])
            label = {"success": "✓ passed", "suspicious": "⚠ checks failed", "failed": "✗ failed"}[res["status"]]
            _activity(agent_id, f"Test run {label} · {len(res['steps'])} steps · {ok_checks}/{len(res['checks'])} checks",
                      {"run_id": res["run_id"], "status": res["status"]})
            return json.dumps(res, default=str)[:15000], False
        if name == "list_runs":
            with session() as s:
                rs = s.scalars(select(Run).where(Run.agent_id == agent_id, Run.kind.in_(REAL_KINDS))
                               .order_by(Run.created_at.desc()).limit(min(int(args.get("limit") or 10), 30))).all()
                return json.dumps([run_brief(r) for r in rs], default=str), False
        if name == "get_run":
            with session() as s:
                r = s.get(Run, args["run_id"])
                if r is None or r.agent_id != agent_id:
                    return "run not found", True
                return json.dumps(runner.run_result(r), default=str)[:15000], False
        if name == "view_memory":
            with session() as s:
                return json.dumps(s.get(Agent, agent_id).state, default=str)[:8000], False
        return f"unknown tool {name}", True
    except (StoreError, KeyError, ValueError) as e:
        return f"Error: {e}", True


def _update_settings(agent_id: str, args: dict) -> str:
    with session() as s:
        a = s.get(Agent, agent_id)
        changed = []
        if args.get("name"):
            a.name = args["name"][:200]
            changed.append("name")
        if "trigger_type" in args:
            if args["trigger_type"] not in ("schedule", "webhook", "manual"):
                raise ValueError("trigger_type must be schedule, webhook or manual")
            a.trigger_type = args["trigger_type"]
            changed.append("trigger")
        if "schedule" in args:
            if not valid_cron(args["schedule"]):
                raise ValueError(f"invalid cron: {args['schedule']}")
            a.schedule = args["schedule"]
            changed.append("schedule")
        if "timezone" in args:
            ZoneInfo(args["timezone"])
            a.timezone = args["timezone"]
        if "alert_email" in args:
            a.alert_email = args["alert_email"][:200]
        if "needed_secrets" in args:
            a.needed_secrets = [x.strip().upper() for x in args["needed_secrets"]][:20]
        for k, lo, hi in (("max_steps", 1, 100), ("max_cost_per_run", 0.0, 20.0), ("timeout_s", 10, 1800)):
            if k in args:
                setattr(a, k, min(max(args[k], lo), hi))
        if a.trigger_type == "schedule" and not a.schedule:
            a.schedule = "0 9 * * *"
        schedule_next(a)
        desc = {"schedule": f"{a.schedule} ({a.timezone})", "webhook": "webhook", "manual": "manual"}[a.trigger_type]
        add_message(s, agent_id, "activity", f"Settings updated · trigger: {desc}")
        return f"Settings saved. Trigger: {desc}. Secrets set: {sorted((a.secrets_enc or {}).keys())}"


def _history(agent_id: str) -> list[dict]:
    with session() as s:
        msgs = s.scalars(select(Message).where(Message.agent_id == agent_id, Message.role.in_(("user", "assistant")))
                         .order_by(Message.id.desc()).limit(14)).all()[::-1]
    out: list[dict] = []
    for m in msgs:
        if out and out[-1]["role"] == m.role:
            out[-1]["content"] += "\n\n" + m.text
        else:
            out.append({"role": m.role, "content": m.text})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def run_session(agent_id: str, mode: str = "build", user_text: str = "", run_id: str | None = None) -> dict:
    """Run the Claude tool loop. Returns {"reply", "changed", "last_test", "files"}."""
    st = config.settings
    client = llm.get_llm()
    with session() as s:
        a = s.get(Agent, agent_id)
        if mode == "repair":
            from .db import Version
            files = s.get(Version, a.live_version_id).files
        else:
            files = a.draft_files or {}
    ws = Workspace(agent_id, files, persist=(mode == "build"))

    if mode == "repair":
        with session() as s:
            r = s.get(Run, run_id)
            detail = json.dumps(runner.run_result(r), default=str)[:15000]
        messages = [{"role": "user", "content": f"{_context(agent_id, ws)}\n\n## Failed run\n```json\n{detail}\n```\n"
                     "Diagnose and repair. Re-test with the same event: "
                     f"{json.dumps((r.input or {}).get('event') or {})}"}]
        system = REPAIR_SYSTEM
    else:
        history = _history(agent_id)
        if history and history[-1]["role"] == "user":  # the new message is already stored
            history[-1]["content"] = f"{_context(agent_id, ws)}\n\n## Owner's request\n{history[-1]['content']}"
        else:
            history.append({"role": "user", "content": f"{_context(agent_id, ws)}\n\n## Owner's request\n{user_text}"})
        messages = history
        system = SYSTEM

    reply = ""
    kind = "repair" if mode == "repair" else "build"
    for turn in range(st.builder_max_turns):
        try:
            resp = client.create(model=st.builder_model, system=system, messages=messages, tools=TOOLS, max_tokens=8000)
        except llm.LLMError as e:
            reply = f"I couldn't reach Claude: {e}"
            break
        usage = resp.get("usage", {})
        with session() as s:
            add_usage(s, agent_id, kind, usage, llm.cost_of(usage))
        blocks = resp.get("content", [])
        messages.append({"role": "assistant", "content": blocks})
        uses = [b for b in blocks if b.get("type") == "tool_use"]
        text = llm.text_of(resp)
        if not uses:
            reply = text
            break
        results = []
        for u in uses:
            out, is_err = _tool(agent_id, ws, u["name"], u.get("input") or {})
            results.append({"type": "tool_result", "tool_use_id": u["id"], "content": out, "is_error": is_err})
        messages.append({"role": "user", "content": results})
    else:
        reply = "I stopped because this took too many steps. Here's where things stand; ask me to continue."

    return {"reply": reply, "changed": ws.changed, "last_test": ws.last_test, "files": ws.files}


def handle_message(agent_id: str, text: str, background: bool = True):
    """Store the owner's message and let the builder answer (in a thread unless background=False)."""
    lock = _locks.setdefault(agent_id, threading.Lock())
    if not lock.acquire(blocking=False):
        raise StoreError("The builder is already working on this agent")
    with session() as s:
        a = s.get(Agent, agent_id)
        add_message(s, agent_id, "user", text)
        a.builder_busy, a.builder_activity = True, "Thinking…"
        if not a.description:
            a.description = text[:2000]

    def work():
        try:
            res = run_session(agent_id, "build", text)
            data = {}
            if res["last_test"]:
                data = {"test_run_id": res["last_test"]["run_id"], "test_status": res["last_test"]["status"]}
            with session() as s:
                add_message(s, agent_id, "assistant", res["reply"] or "Done.", data)
        except Exception as e:  # noqa: BLE001
            log.exception("builder failed")
            with session() as s:
                add_message(s, agent_id, "assistant", f"Something went wrong while building: {e}")
        finally:
            with session() as s:
                a = s.get(Agent, agent_id)
                a.builder_busy, a.builder_activity = False, ""
            lock.release()

    if background:
        threading.Thread(target=work, daemon=True, name=f"builder-{agent_id}").start()
    else:
        work()


def outline(files: dict) -> list[dict]:
    return steps_from_code(files.get("agent.py", "")) if files.get("agent.py") else []
