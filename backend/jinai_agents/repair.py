"""What happens after a run fails or a check fails: try to repair, otherwise pause and tell the owner."""
from __future__ import annotations

import logging
import re
import threading
from datetime import timedelta

from sqlalchemy import func, select

from . import config, notify
from .db import Agent, Event, Run, now, session
from .store import StoreError, add_message, deploy

log = logging.getLogger("jinai.repair")
FAILURES_BEFORE_PAUSE = 3


def repairs_last_day(s, agent_id: str) -> int:
    return s.scalar(select(func.count()).select_from(Run).where(
        Run.agent_id == agent_id, Run.repair_status.in_(("repairing", "fixed", "not_fixed")),
        Run.created_at >= now() - timedelta(days=1))) or 0


def after_bad_run(run_id: str, background: bool = True):
    with session() as s:
        run = s.get(Run, run_id)
        agent = s.get(Agent, run.agent_id)
        if agent.status != "live":
            return
        can_repair = (agent.auto_repair and repairs_last_day(s, agent.id) < config.settings.repairs_per_day
                      and not agent.builder_busy and bool(config.settings.anthropic_api_key or _llm_is_set()))
        if can_repair:
            run.repair_status = "repairing"
        elif agent.consecutive_failures >= FAILURES_BEFORE_PAUSE:
            _needs_you(s, agent, run, f"Failed {agent.consecutive_failures} runs in a row. Last error: "
                                      f"{_short(run)}", )
            return
        else:
            return
    if background:
        threading.Thread(target=repair, args=(run_id,), daemon=True, name=f"repair-{run_id}").start()
    else:
        repair(run_id)


def _llm_is_set() -> bool:
    from . import llm
    return llm._llm is not None and not isinstance(llm._llm, llm.AnthropicLLM)


def _short(run: Run) -> str:
    if run.status == "failed":
        return (run.error.strip().splitlines() or ["unknown error"])[0][:200]
    bad = [c["message"] for c in run.checks if not c.get("ok")]
    return "check failed: " + (bad[0] if bad else "?")


def _needs_you(s, agent: Agent, run: Run | None, reason: str):
    agent.status = "needs_attention"
    agent.next_run_at = None
    s.add(Event(agent_id=agent.id, kind="needs_you", text=reason[:2000], run_id=run.id if run else None))
    add_message(s, agent.id, "assistant", f"⚠ I paused this agent. {reason}\n\nTell me how you'd like to "
                                          "proceed, or fix it in the editor and deploy again.")
    notify.alert_owner(agent, "needs your attention", f"{reason}\n\nThe agent is paused until you deploy a fix "
                                                      "or resume it.")


def repair(run_id: str):
    from .builder import _locks, run_session
    with session() as s:
        run = s.get(Run, run_id)
        agent_id = run.agent_id
        event = (run.input or {}).get("event") or {}
        kind = run.kind
    lock = _locks.setdefault(agent_id, threading.Lock())
    if not lock.acquire(blocking=False):
        with session() as s:
            s.get(Run, run_id).repair_status = ""
        return
    try:
        with session() as s:
            a = s.get(Agent, agent_id)
            a.builder_busy, a.builder_activity = True, "Auto-repairing a failed run…"
            add_message(s, agent_id, "activity", f"Run failed ({_short(s.get(Run, run_id))}). Trying to repair…",
                        {"run_id": run_id})
        res = run_session(agent_id, "repair", run_id=run_id)
        reply = res["reply"] or ""
        m = re.search(r"(NOT FIXED|FIXED):\s*(.+)", reply)
        verdict, reason = (m.group(1), m.group(2).strip()) if m else ("", reply.strip()[-300:])
        fixed = (verdict == "FIXED" and res["changed"] and res["last_test"]
                 and res["last_test"]["status"] == "success")
        with session() as s:
            agent = s.get(Agent, agent_id)
            run = s.get(Run, run_id)
            if fixed:
                try:
                    v = deploy(s, agent, res["files"], f"Auto-fix: {reason}"[:300], "repair")
                except StoreError as e:
                    fixed, reason = False, f"fix could not be deployed: {e}"
                else:
                    run.repair_status = "fixed"
                    if not agent.draft_dirty:
                        agent.draft_files = dict(res["files"])
                    explain = reply.split("FIXED:")[0].strip()
                    add_message(s, agent_id, "assistant", f"✓ Auto-repaired and deployed v{v.number}. {reason}"
                                + (f"\n\n{explain}" if explain else ""), {"run_id": run_id})
            if not fixed:
                run.repair_status = "not_fixed"
                _needs_you(s, agent, run, f"{_short(run)}. {reason}")
        if fixed and kind in ("schedule", "manual", "webhook"):
            from .runner import RunnerError, queue_run
            try:
                queue_run(agent_id, kind if kind != "schedule" else "manual", event)  # redo the missed run
            except RunnerError:
                pass
    except Exception as e:  # noqa: BLE001
        log.exception("repair failed")
        with session() as s:
            s.get(Run, run_id).repair_status = "not_fixed"
            add_message(s, agent_id, "activity", f"Repair attempt crashed: {e}")
    finally:
        with session() as s:
            a = s.get(Agent, agent_id)
            a.builder_busy, a.builder_activity = False, ""
        lock.release()
