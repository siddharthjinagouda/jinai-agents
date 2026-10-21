"""Test harness: a real Jinai server on a free port, a fake website, and a scripted Claude."""
from __future__ import annotations

import http.server
import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

TMP = Path(tempfile.mkdtemp(prefix="jinai-test-"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
os.environ.update({
    "JINAI_DATABASE_URL": os.environ.get("TEST_DATABASE_URL", f"sqlite:///{TMP}/jinai.db"), "JINAI_DATA_DIR": str(TMP / "data"),
    "JINAI_ADMIN_TOKEN": "test-token", "JINAI_INTERNAL_URL": f"http://127.0.0.1:{PORT}",
    "JINAI_PUBLIC_URL": f"http://127.0.0.1:{PORT}", "JINAI_OWNER_EMAIL": "owner@example.com",
    "JINAI_SCHEDULER": "0", "ANTHROPIC_API_KEY": "", "JINAI_SECRET_KEY": "",
})

from jinai_agents import config  # noqa: E402

config.reload()

from jinai_agents import llm  # noqa: E402
from jinai_agents.llm import ScriptedLLM  # noqa: E402


# --------------------------------------------------------------------------- fake website
class Site:
    routes: dict[str, tuple[int, str]] = {}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = Site.routes.get(self.path, (404, "not found"))
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json" if body.startswith(("{", "[")) else "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


SITE_PORT = free_port()
_site = http.server.ThreadingHTTPServer(("127.0.0.1", SITE_PORT), _Handler)
threading.Thread(target=_site.serve_forever, daemon=True).start()
SITE = f"http://127.0.0.1:{SITE_PORT}"


# --------------------------------------------------------------------------- scripted Claude
class Script:
    """Queues of responses. Builder calls (with write_file tool) pop from build/repair; agent calls use runtime."""
    build: list = []
    repair: list = []
    runtime = None
    seen: list = []

    @classmethod
    def reset(cls):
        cls.build, cls.repair, cls.runtime, cls.seen = [], [], None, []


def tool_use(_name, **inp):
    name = _name
    return {"type": "tool_use", "id": f"tu_{name}_{time.time_ns()}", "name": name, "input": inp}


def text(t):
    return {"type": "text", "text": t}


def last_tool_result(messages) -> dict | None:
    m = messages[-1]
    if m["role"] == "user" and isinstance(m["content"], list):
        for b in m["content"]:
            if b.get("type") == "tool_result":
                try:
                    return json.loads(b["content"])
                except (ValueError, TypeError):
                    return {"raw": b["content"]}
    return None


def responder(system, messages, tools):
    Script.seen.append({"system": system[:80], "n": len(messages), "tools": [t["name"] for t in tools or []]})
    is_builder = tools and any(t["name"] == "write_file" for t in tools)
    if is_builder:
        q = Script.repair if "REPAIR mode" in system else Script.build
        if not q:
            return {"content": [text("(script exhausted)")]}
        step = q.pop(0)
        blocks = step(messages) if callable(step) else step
        return {"content": blocks}
    if Script.runtime is None:
        return {"content": [text("ok")]}
    return {"content": Script.runtime(system, messages, tools)}


llm.set_llm(ScriptedLLM(responder))


# --------------------------------------------------------------------------- server
@pytest.fixture(scope="session")
def server():
    import uvicorn

    from jinai_agents.api import app
    cfg = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    srv = uvicorn.Server(cfg)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{PORT}"
    srv.should_exit = True


@pytest.fixture()
def api(server):
    import httpx
    Script.reset()
    with httpx.Client(base_url=server, headers={"Authorization": "Bearer test-token"}, timeout=60) as c:
        yield c


def wait_for(fn, timeout=30, interval=0.2):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting; last value: {last!r}")
