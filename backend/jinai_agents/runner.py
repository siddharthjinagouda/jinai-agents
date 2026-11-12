"""Runs an agent once, in a sandbox, and records what happened."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select

from . import config, notify
from .db import Agent, Run, Version, WorkspaceSecret, now, session
from .store import REAL_KINDS, live_version, secret_env

LOG_LIMIT = 60_000
_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def pool() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=config.settings.workers, thread_name_prefix="jinai-run")
        return _pool


class RunnerError(Exception):
    pass


# --------------------------------------------------------------------------- public API
def queue_run(agent_id: str, kind: str, event: dict | None = None) -> str:
    """Create a run for the live version and execute it in the background. Returns run id."""
    with session() as s:
        agent = s.get(Agent, agent_id)
        if agent is None:
            raise RunnerError("agent not found")
        if not agent.live_version_id:
            raise RunnerError("agent has no deployed version yet")
        busy = s.scalar(select(Run.id).where(Run.agent_id == agent_id, Run.kind.in_(REAL_KINDS),
                                             Run.status.in_(("queued", "running"))).limit(1))
        if busy and kind == "schedule":
            raise RunnerError("previous run still in progress")
        v = live_version(s, agent)
        run = Run(agent_id=agent_id, kind=kind, input={"event": event or {}}, version_number=v.number)
        s.add(run)
        s.flush()
        run_id = run.id
    pool().submit(_safe_execute, run_id)
    return run_id


def test_run(agent_id: str, files: dict, event: dict | None = None) -> dict:
    """Run draft files synchronously (builder and 'Test run' button). Nothing is sent, memory is not saved."""
    with session() as s:
        run = Run(agent_id=agent_id, kind="test", input={"event": event or {}})
        s.add(run)
        s.flush()
        run_id = run.id
    execute(run_id, files)
    with session() as s:
        return run_result(s.get(Run, run_id))


def _safe_execute(run_id: str):
    try:
        execute(run_id)
    except Exception as e:  # never kill the worker
        with session() as s:
            r = s.get(Run, run_id)
            if r and r.status in ("queued", "running"):
                r.status, r.error, r.finished_at = "failed", f"Runner error: {e}", now()


# --------------------------------------------------------------------------- execution
def execute(run_id: str, files: dict | None = None):
    st = config.settings
    with session() as s:
        run = s.get(Run, run_id)
        agent = s.get(Agent, run.agent_id)
        if files is None:
            v = s.get(Version, agent.live_version_id)
            files = v.files
        run.status, run.started_at = "running", now()
        env_secrets = secret_env(agent)
        # Also load workspace-level secrets (prefixed JINAI_WSECRET_)
        ws_secrets = s.scalars(select(WorkspaceSecret)).all()
        from .store import _fernet
        f = _fernet()
        for ws in ws_secrets:
            try:
                env_secrets["JINAI_WSECRET_" + ws.name] = f.decrypt(ws.encrypted_value.encode()).decode()
            except Exception:
                pass
        state_in = agent.state or {}
        timeout = agent.timeout_s
        max_steps = agent.max_steps
        token = run.token
        event = (run.input or {}).get("event") or {}
        tool_config = agent.tool_config or {}
        # Load checkpoint if the run is resuming
        checkpoint_data = run.human_input  # reuse human_input field for checkpoint storage

    work = st.data_dir / "runs" / run_id
    agent_dir, io_dir = work / "agent", work / "io"
    shutil.rmtree(work, ignore_errors=True)
    agent_dir.mkdir(parents=True)
    io_dir.mkdir()
    for path, content in files.items():
        p = agent_dir / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    (io_dir / "input.json").write_text(json.dumps({"event": event}, default=str), encoding="utf-8")
    (io_dir / "state_in.json").write_text(json.dumps(state_in, default=str), encoding="utf-8")
    if checkpoint_data and isinstance(checkpoint_data, dict) and checkpoint_data.get("_checkpoint"):
        (io_dir / "checkpoint.json").write_text(json.dumps(checkpoint_data), encoding="utf-8")

    import json as _json
    env = {"JINAI_RUN_TOKEN": token, "JINAI_MAX_STEPS": str(max_steps), "LANG": "C.UTF-8",
           "PYTHONUNBUFFERED": "1", "JINAI_TOOL_CONFIG": _json.dumps(tool_config), **env_secrets}
    if st.runner == "docker":
        rc, logs, timed_out = _run_docker(run_id, agent_dir, io_dir, env, timeout)
    else:
        rc, logs, timed_out = _run_process(agent_dir, io_dir, env, timeout)

    events = _read_events(io_dir / "events.jsonl")
    steps, checks, notes = _assemble(events)
    result = _read_json(io_dir / "result.json")
    error = _read_json(io_dir / "error.json")
    state_out = _read_json(io_dir / "state_out.json")

    if timed_out:
        status, err_text = "failed", f"Timed out after {timeout}s"
    elif error or rc != 0:
        status = "failed"
        err_text = (f"{error['type']}: {error['message']}\n\n{error.get('traceback', '')}" if error
                    else f"Agent process exited with code {rc}")
    else:
        status = "suspicious" if any(not c["ok"] for c in checks) else "success"
        err_text = ""

    with session() as s:
        run = s.get(Run, run_id)
        agent = s.get(Agent, run.agent_id)
        real = run.kind in REAL_KINDS
        for n in notes:
            if not real:
                n["status"] = "test (not sent)"
            elif status == "success":
                n["status"] = notify.send(n.get("to") or agent.alert_email or st.owner_email,
                                          n.get("subject") or f"[{agent.name}]", n["message"])
            else:
                n["status"] = "held (run " + status + ")"
        run.status = status
        run.steps, run.checks, run.notifications = steps, checks, notes
        run.output = (result or {}).get("output")
        run.error = err_text[:20000]
        run.logs = logs[-LOG_LIMIT:]
        run.finished_at = now()
        if real:
            if status == "success":
                agent.consecutive_failures = 0
                if state_out is not None:
                    agent.state = state_out
            else:
                agent.consecutive_failures += 1
        follow_up = real and status != "success"

    shutil.rmtree(work, ignore_errors=True)
    if follow_up:
        from .repair import after_bad_run
        after_bad_run(run_id)


def _limits(timeout: int):
    def apply():
        import resource
        os.setsid()
        resource.setrlimit(resource.RLIMIT_CPU, (timeout + 5, timeout + 10))
        resource.setrlimit(resource.RLIMIT_FSIZE, (100 << 20, 100 << 20))
    return apply


def _run_process(agent_dir: Path, io_dir: Path, env: dict, timeout: int):
    """Local subprocess. Fine for your own agents; use the docker runner for anyone else's code."""
    full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(io_dir),
                "JINAI_IO": str(io_dir), "JINAI_API": config.settings.internal_url, **env}
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        full_env["PLAYWRIGHT_BROWSERS_PATH"] = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    cmd = [sys.executable, "-I", str(config.SANDBOX_LIB / "boot.py"), str(agent_dir)]
    p = subprocess.Popen(cmd, cwd=agent_dir, env=full_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         preexec_fn=_limits(timeout))
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out.decode("utf-8", "replace"), False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = p.communicate()
        return -9, out.decode("utf-8", "replace"), True


def docker_command(run_id: str, agent_dir: Path, io_dir: Path, env: dict) -> list[str]:
    st = config.settings
    cmd = ["docker", "run", "--rm", "--name", f"jinai-{run_id}",
           "--memory", "768m", "--cpus", "1", "--pids-limit", "256",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
           "--tmpfs", "/tmp:rw,size=128m", "--add-host", "host.docker.internal:host-gateway",
           "-v", f"{agent_dir}:/agent:ro", "-v", f"{io_dir}:/io:rw", "-v", f"{config.SANDBOX_LIB}:/jinai:ro",
           "-e", "JINAI_IO=/io", "-e", f"JINAI_API={st.internal_url}", "-e", "HOME=/tmp"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    return cmd + ["--workdir", "/agent", st.runner_image, "python", "-I", "/jinai/boot.py", "/agent"]


def _run_docker(run_id: str, agent_dir: Path, io_dir: Path, env: dict, timeout: int):
    os.chmod(io_dir, 0o777)  # container user writes events/state
    cmd = docker_command(run_id, agent_dir, io_dir, env)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 30)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace"), False
    except subprocess.TimeoutExpired as e:
        subprocess.run(["docker", "kill", f"jinai-{run_id}"], capture_output=True)
        return -9, ((e.stdout or b"") + (e.stderr or b"")).decode("utf-8", "replace"), True


# --------------------------------------------------------------------------- reading results
def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except ValueError:
        return None


def _read_events(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[:20000]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _assemble(events: list[dict]):
    steps: dict[int, dict] = {}
    order: list[int] = []
    checks, notes = [], []
    for e in events:
        k = e.get("type")
        if k == "step_start":
            parent = e.get("parent")
            depth = steps[parent]["depth"] + 1 if parent in steps else 0
            steps[e["id"]] = {"id": e["id"], "parent": parent, "depth": depth, "name": e.get("name", ""),
                              "status": "running", "started": e["t"], "duration_s": None, "notes": [],
                              "error": "", "ai_calls": 0}
            order.append(e["id"])
        elif k == "step_note" and e.get("id") in steps:
            steps[e["id"]]["notes"].append(e.get("text", ""))
        elif k == "step_end" and e.get("id") in steps:
            st = steps[e["id"]]
            st["status"] = e.get("status", "ok")
            st["error"] = e.get("error", "")
            st["duration_s"] = round(e["t"] - st["started"], 2)
        elif k == "check":
            checks.append({"ok": bool(e.get("ok")), "message": e.get("message", ""), "step": e.get("step")})
        elif k == "notify":
            notes.append({"message": e.get("message", ""), "subject": e.get("subject", ""), "to": e.get("to", "")})
        elif k == "ai_call":
            open_steps = [i for i in order if steps[i]["status"] == "running"]
            if open_steps:
                steps[open_steps[-1]]["ai_calls"] += 1
    out = []
    for i in order:
        st = steps[i]
        if st["status"] == "running":
            st["status"] = "interrupted"
        st.pop("started", None)
        out.append(st)
    return out, checks, notes


def run_result(r: Run) -> dict:
    """Compact view of a run for the builder and API."""
    return {"run_id": r.id, "status": r.status, "kind": r.kind,
            "steps": [{"name": s["name"], "status": s["status"], "depth": s["depth"], "notes": s["notes"][-3:],
                       "error": s["error"]} for s in r.steps][:60],
            "checks": r.checks[:40], "notifications": r.notifications[:10],
            "output": r.output, "error": r.error[-4000:], "logs_tail": r.logs[-3000:],
            "ai_calls": r.ai_calls, "ai_cost": round(r.ai_cost, 4)}
