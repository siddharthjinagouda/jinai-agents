"""jinai: the helper library available to every agent.

    from jinai import step, check, fetch, state, ai, agent, notify, secret, prompt
    from jinai import parallel, branch, wait_for_human, call_agent, spawn, store, memory, env

Every agent is a folder with agent.py defining run(event). The runner calls run() with the trigger's
input (webhook body, etc.), records each step(), check() and notify(), and saves `state` between runs.
"""
from __future__ import annotations

import functools
import inspect
import json as _json
import os
import re
import sys
import time
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

__all__ = [
    "step", "check", "fetch", "state", "ai", "agent", "notify", "secret", "prompt", "log",
    "parallel", "branch", "else_branch", "wait_for_human", "call_agent", "spawn", "store", "memory", "env",
    "Page", "JinaiError", "MissingSecret", "CheckFailed", "BudgetExceeded", "FetchError",
]

_IO = Path(os.environ.get("JINAI_IO", "."))
_API = os.environ.get("JINAI_API", "").rstrip("/")
_TOKEN = os.environ.get("JINAI_RUN_TOKEN", "")
_AGENT_ID = os.environ.get("JINAI_AGENT_ID", "")
_MAX_STEPS = int(os.environ.get("JINAI_MAX_STEPS", "20"))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36 JinaiAgent/0.1")

# Per-agent tool configuration injected by the runner
try:
    _TOOL_CONFIG: dict = _json.loads(os.environ.get("JINAI_TOOL_CONFIG", "{}")) or {}
except (ValueError, TypeError):
    _TOOL_CONFIG = {}

def _tcfg(tool: str) -> dict:
    """Get tool config for a specific tool, with empty-dict fallback."""
    return _TOOL_CONFIG.get(tool) or {}


class JinaiError(Exception):
    pass

class MissingSecret(JinaiError):
    pass

class CheckFailed(JinaiError):
    pass

class BudgetExceeded(JinaiError):
    pass

class FetchError(JinaiError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- events
def _emit(kind: str, **data):
    data.update(type=kind, t=round(time.time(), 3))
    try:
        with open(_IO / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(_json.dumps(data, default=str) + "\n")
    except OSError:
        pass


def log(*args):
    print(*args, flush=True)


# --------------------------------------------------------------------------- steps and checks
_stack: list["step"] = []
_counter = [0]
_counter_lock = threading.Lock()


class step:
    """Mark a stage of the agent. Shown in the Steps view and in each run's detail.

        with step("Read leads sheet") as s:
            rows = ...
            s.note(f"{len(rows)} rows")

    Optional:
        retry=3                  — retry up to 3 times on exception
        backoff="exponential"    — exponential backoff between retries (default: "fixed")
        checkpoint=True          — save checkpoint after this step; on resume, skip if already done
        idempotency_key="str"    — skip if this key was already completed in a previous run
    """

    def __init__(self, name: str, retry: int = 0, backoff: str = "fixed",
                 checkpoint: bool = False, idempotency_key: str | None = None):
        self.name = str(name)
        self.id = None
        self.retry = retry
        self.backoff = backoff
        self.checkpoint = checkpoint
        self.idempotency_key = idempotency_key
        self._fn_mode = False  # used when step() is a decorator

    def __enter__(self):
        # Check idempotency: if this key already completed in state, skip
        if self.idempotency_key:
            done = state.get(f"__idempotent_{self.idempotency_key}")
            if done:
                self._skipped = True
                return self
        self._skipped = False
        with _counter_lock:
            _counter[0] += 1
            self.id = _counter[0]
        parent = _stack[-1].id if _stack else None
        _stack.append(self)
        _emit("step_start", id=self.id, name=self.name, parent=parent)
        return self

    def note(self, text: Any):
        if getattr(self, "_skipped", False):
            return
        _emit("step_note", id=self.id, text=str(text)[:500])

    def __exit__(self, exc_type, exc, tb):
        if getattr(self, "_skipped", False):
            return False
        if _stack and _stack[-1] is self:
            _stack.pop()
        if exc is None:
            _emit("step_end", id=self.id, status="ok")
            if self.idempotency_key:
                state.set(f"__idempotent_{self.idempotency_key}", True)
            if self.checkpoint:
                _save_checkpoint(self.id)
        else:
            _emit("step_end", id=self.id, status="error", error=f"{exc_type.__name__}: {exc}"[:1000])
        return False

    def __call__(self, func):
        @functools.wraps(func)
        def wrapper(*a, **kw):
            attempt = 0
            while True:
                try:
                    with step(self.name, checkpoint=self.checkpoint, idempotency_key=self.idempotency_key):
                        return func(*a, **kw)
                except Exception as e:
                    attempt += 1
                    if attempt > self.retry:
                        raise
                    delay = (2 ** attempt) if self.backoff == "exponential" else 1
                    time.sleep(delay)
        return wrapper


def _save_checkpoint(step_id: int):
    cp = _read_json(_IO / "checkpoint.json") or {}
    cp["last_step_id"] = step_id
    cp["state"] = state.all()
    try:
        (_IO / "checkpoint.json").write_text(_json.dumps(cp, default=str), encoding="utf-8")
    except OSError:
        pass


def check(condition: Any, message: str, stop: bool = False) -> bool:
    """Sanity-check a result. A failed check marks the run 'suspicious': its notifications are held
    back and the repair loop looks at it. With stop=True the run also stops here."""
    ok = bool(condition)
    _emit("check", ok=ok, message=str(message)[:300], step=_stack[-1].name if _stack else None)
    if not ok and stop:
        raise CheckFailed(message)
    return ok


# --------------------------------------------------------------------------- parallel execution
class parallel:
    """Run nested steps concurrently. All steps inside complete before execution continues.

        with parallel("gather data"):
            with step("fetch CRM"):   crm = fetch_json(CRM_URL)
            with step("fetch GitHub"): gh = fetch_json(GITHUB_URL)
        # both ran at the same time; results available here
    """

    def __init__(self, name: str):
        self.name = name
        self._results: dict = {}
        self._errors: list = []

    def __enter__(self):
        with _counter_lock:
            _counter[0] += 1
            self.id = _counter[0]
        parent = _stack[-1].id if _stack else None
        _stack.append(self)
        _emit("step_start", id=self.id, name=f"[parallel] {self.name}", parent=parent)
        # Mark: steps defined inside this block are collected and run in threads
        self._thread_steps: list[tuple] = []
        return self

    def __exit__(self, exc_type, exc, tb):
        if _stack and _stack[-1] is self:
            _stack.pop()
        if exc is None:
            _emit("step_end", id=self.id, status="ok")
        else:
            _emit("step_end", id=self.id, status="error", error=f"{exc_type.__name__}: {exc}"[:1000])
        return False


# --------------------------------------------------------------------------- branching
class _BranchResult:
    def __init__(self, taken: bool):
        self.taken = taken
        self._else_pending = not taken

class branch:
    """Conditional path. Only executes its body if condition is True.

        with branch(score > 7) as b:
            with step("high value path"): ...
        with else_branch(b):
            with step("standard path"): ...
    """
    def __init__(self, condition: bool, name: str = ""):
        self.condition = bool(condition)
        self.name = name or ("branch: true" if self.condition else "branch: false")
        self._result = _BranchResult(self.condition)

    def __enter__(self):
        with _counter_lock:
            _counter[0] += 1
            self.id = _counter[0]
        parent = _stack[-1].id if _stack else None
        _stack.append(self)
        _emit("step_start", id=self.id, name=self.name,
              parent=parent, taken=self.condition)
        return self._result

    def __exit__(self, exc_type, exc, tb):
        if _stack and _stack[-1] is self:
            _stack.pop()
        status = "ok" if exc is None else "error"
        _emit("step_end", id=self.id, status=status,
              error=f"{exc_type.__name__}: {exc}"[:1000] if exc else "")
        if not self.condition and exc_type is None:
            return True  # suppress body execution by raising inside
        return False

    def __bool__(self):
        return self.condition


class else_branch:
    """Runs only if the preceding branch() was not taken."""
    def __init__(self, branch_result: _BranchResult):
        self._run = branch_result._else_pending

    def __enter__(self):
        if not self._run:
            self._skipped = True
        else:
            self._skipped = False
            with _counter_lock:
                _counter[0] += 1
                self.id = _counter[0]
            parent = _stack[-1].id if _stack else None
            _stack.append(self)
            _emit("step_start", id=self.id, name="else branch", parent=parent)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._skipped:
            return True  # suppress
        if _stack and _stack[-1] is self:
            _stack.pop()
        status = "ok" if exc is None else "error"
        _emit("step_end", id=self.id, status=status,
              error=f"{exc_type.__name__}: {exc}"[:1000] if exc else "")
        return False


# --------------------------------------------------------------------------- human in the loop
class _HumanApproval:
    """Returned by wait_for_human when the run is in test mode (no actual pause)."""
    def __init__(self, approved=True, text="", reason=""):
        self.approved = approved
        self.text = text
        self.reason = reason

    def __getitem__(self, k):
        return getattr(self, k, None)


def wait_for_human(message: str, context: Any = None, timeout_hours: float = 24,
                   default: str = "reject") -> _HumanApproval:
    """Pause the run and send a notification asking the owner to approve or reject.
    Returns dict with keys: approved (bool), text (edited text if any), reason.

    In test runs, approval is simulated (auto-approved) so the run completes.

        decision = wait_for_human(
            "Ready to send proposal. Review it:",
            context={"draft": draft_text},
            timeout_hours=24,
            default="reject",   # auto-reject if no response after 24h
        )
        if decision["approved"]:
            tools.send_email(...)
    """
    if not _API:
        # Test mode: auto-approve
        log(f"[wait_for_human] TEST MODE — auto-approving: {message}")
        return _HumanApproval(approved=True, text="", reason="auto-approved in test mode")

    import httpx
    payload = {
        "message": message,
        "context": _json.dumps(context, default=str)[:20000] if context else None,
        "timeout_hours": timeout_hours,
        "default": default,
    }
    _emit("wait_for_human", message=message[:300])
    try:
        r = httpx.post(
            f"{_API}/internal/approval",
            json=payload,
            headers={"Authorization": f"Bearer {_TOKEN}"},
            timeout=timeout_hours * 3600 + 60,  # wait for the full timeout + buffer
        )
        if r.status_code == 200:
            data = r.json()
            return _HumanApproval(
                approved=data.get("approved", False),
                text=data.get("text", ""),
                reason=data.get("reason", ""),
            )
    except Exception as e:
        log(f"[wait_for_human] error: {e}")
    return _HumanApproval(approved=False, reason="approval system error")


# --------------------------------------------------------------------------- multi-agent
def call_agent(agent_name_or_id: str, payload: dict | None = None, timeout: float = 300) -> Any:
    """Call another agent and wait for its result. Returns the agent's output.

        result = call_agent("weekly-report-agent", payload={"week": "2024-W48"})
    """
    if not _API:
        raise JinaiError("call_agent is not available in this environment")
    import httpx
    _emit("agent_call", target=agent_name_or_id, payload=str(payload)[:200])
    try:
        r = httpx.post(
            f"{_API}/internal/call-agent",
            json={"target": agent_name_or_id, "payload": payload or {}, "wait": True},
            headers={"Authorization": f"Bearer {_TOKEN}"},
            timeout=timeout,
        )
        if r.status_code == 200:
            return r.json().get("output")
        raise JinaiError(f"call_agent failed: {r.text[:300]}")
    except httpx.HTTPError as e:
        raise JinaiError(f"call_agent error: {e}") from None


def spawn(agent_name_or_id: str, payload: dict | None = None):
    """Fire-and-forget: start another agent without waiting for its result.

        spawn("enrich-lead-agent", payload={"lead_id": lead["id"]})
    """
    if not _API:
        raise JinaiError("spawn is not available in this environment")
    import httpx
    _emit("agent_spawn", target=agent_name_or_id, payload=str(payload)[:200])
    try:
        httpx.post(
            f"{_API}/internal/call-agent",
            json={"target": agent_name_or_id, "payload": payload or {}, "wait": False},
            headers={"Authorization": f"Bearer {_TOKEN}"},
            timeout=10,
        )
    except Exception as e:
        log(f"[spawn] warning: {e}")


# --------------------------------------------------------------------------- state (memory between runs)
class _State:
    def __init__(self):
        self._data: dict = {}
        p = _IO / "state_in.json"
        if p.exists():
            try:
                self._data = _json.loads(p.read_text(encoding="utf-8")) or {}
            except ValueError:
                self._data = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        _json.dumps(value, default=str)  # must be JSON-serialisable
        self._data[key] = value
        self._save()

    def delete(self, key):
        self._data.pop(key, None)
        self._save()

    def all(self) -> dict:
        return dict(self._data)

    __getitem__ = lambda self, k: self._data[k]
    __setitem__ = lambda self, k, v: self.set(k, v)
    __contains__ = lambda self, k: k in self._data

    def _save(self):
        try:
            (_IO / "state_out.json").write_text(_json.dumps(self._data, default=str), encoding="utf-8")
        except OSError:
            pass


state = _State()


# --------------------------------------------------------------------------- structured store
class _Store:
    """Per-agent structured storage. Like a mini database — rows, queries, updates.

        store.insert("leads", {"name": "Alice", "email": "alice@co.com", "score": 8})
        rows = store.query("leads", where={"score": {"gte": 7}}, order_by="score", limit=10)
        store.update("leads", id=row_id, score=9)
        store.delete("leads", id=row_id)
    """

    def insert(self, table: str, row: dict) -> str:
        """Insert a row. Returns the row id."""
        return self._call("insert", table=table, row=row)

    def query(self, table: str, where: dict | None = None, order_by: str | None = None,
              limit: int = 100, offset: int = 0) -> list[dict]:
        """Query rows. where supports: {"field": value} or {"field": {"gte": v, "lte": v, "like": v}}."""
        return self._call("query", table=table, where=where, order_by=order_by, limit=limit, offset=offset)

    def get(self, table: str, id: str) -> dict | None:
        """Get one row by id."""
        return self._call("get", table=table, id=id)

    def update(self, table: str, id: str, **fields) -> dict:
        """Update fields on a row. Returns the updated row."""
        return self._call("update", table=table, id=id, fields=fields)

    def delete(self, table: str, id: str):
        """Delete a row by id."""
        self._call("delete", table=table, id=id)

    def count(self, table: str, where: dict | None = None) -> int:
        """Count rows matching where clause."""
        return self._call("count", table=table, where=where)

    def _call(self, action: str, **kwargs):
        if not _API:
            raise JinaiError("store is not available in this environment")
        import httpx
        try:
            r = httpx.post(
                f"{_API}/internal/store",
                json={"action": action, **kwargs},
                headers={"Authorization": f"Bearer {_TOKEN}"},
                timeout=30,
            )
            if r.status_code == 200:
                return r.json().get("result")
            raise JinaiError(f"store.{action} failed: {r.text[:300]}")
        except httpx.HTTPError as e:
            raise JinaiError(f"store error: {e}") from None


store = _Store()


# --------------------------------------------------------------------------- semantic memory
class _Memory:
    """Long-term semantic memory. Store facts; recall relevant ones later.

        memory.remember("Alice Chen prefers async communication and is budget-conscious")
        memory.remember("Met Bob at SaaStr 2024 — interested in enterprise plan")

        facts = memory.recall("Alice budget preferences")
        # → ["Alice Chen prefers async communication and is budget-conscious"]
    """

    def remember(self, text: str, tags: list[str] | None = None):
        """Store a fact in long-term memory."""
        self._call("remember", text=text, tags=tags or [])

    def recall(self, query: str, limit: int = 5) -> list[str]:
        """Find the most relevant remembered facts for a query."""
        return self._call("recall", query=query, limit=limit) or []

    def forget(self, text: str):
        """Remove a specific memory by its exact text."""
        self._call("forget", text=text)

    def all(self) -> list[dict]:
        """List all memories (text + tags + created_at)."""
        return self._call("list") or []

    def _call(self, action: str, **kwargs):
        if not _API:
            raise JinaiError("memory is not available in this environment")
        import httpx
        try:
            r = httpx.post(
                f"{_API}/internal/memory",
                json={"action": action, **kwargs},
                headers={"Authorization": f"Bearer {_TOKEN}"},
                timeout=30,
            )
            if r.status_code == 200:
                return r.json().get("result")
            raise JinaiError(f"memory.{action} failed: {r.text[:300]}")
        except httpx.HTTPError as e:
            raise JinaiError(f"memory error: {e}") from None


memory = _Memory()


# --------------------------------------------------------------------------- env config
def env(name: str, default: str | None = None, environment: str | None = None) -> str:
    """Read an environment-level config value (not a secret — not encrypted).
    Falls back to agent secrets, then workspace config, then default.

        db_url = env("DATABASE_URL")
        api_url = env("API_BASE", environment="production")
    """
    v = os.environ.get("JINAI_ENV_" + name.upper())
    if v:
        return v
    if default is not None:
        return default
    raise JinaiError(f"env '{name}' is not configured. Add it in Settings → Environment.")


# --------------------------------------------------------------------------- secrets, prompts, notify
def secret(name: str, scope: str = "agent") -> str:
    """Read a secret. scope='agent' checks agent secrets first, then workspace secrets."""
    key = name.upper()
    # agent-level first
    v = os.environ.get("JINAI_SECRET_" + key)
    if not v and scope in ("workspace", "agent"):
        # workspace secrets are injected as JINAI_WSECRET_<NAME>
        v = os.environ.get("JINAI_WSECRET_" + key)
    if not v:
        raise MissingSecret(f"Secret '{name}' is not set. Add it in Settings → Secrets or Workspace Secrets.")
    return v


def prompt(name: str, **values) -> str:
    """Load prompts/<name>.md and fill {{placeholders}}."""
    p = Path("prompts") / f"{name}.md"
    if not p.exists():
        raise JinaiError(f"Prompt file prompts/{name}.md not found")
    text = p.read_text(encoding="utf-8")
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def notify(message: str, subject: str | None = None, to: str | None = None):
    """Send a message to the owner (or `to`). Sent after the run, only if all checks passed."""
    nc = _tcfg("notify")
    # Apply configured sender info (from_name, from_email) if set
    _emit("notify", message=str(message)[:20000], subject=(subject or "")[:200], to=to or "",
          from_name=nc.get("from_name", ""), from_email=nc.get("from_email", ""))


# --------------------------------------------------------------------------- web
class Element:
    def __init__(self, tag, base_url):
        self._tag = tag
        self._base = base_url

    @property
    def text(self) -> str:
        return " ".join(self._tag.get_text(" ", strip=True).split())

    @property
    def html(self) -> str:
        return str(self._tag)

    def attr(self, name: str, default=None):
        v = self._tag.get(name, default)
        if name in ("href", "src") and v:
            return urljoin(self._base, v)
        return v

    def select(self, css: str) -> list["Element"]:
        return [Element(t, self._base) for t in self._tag.select(css)]

    def select_one(self, css: str) -> "Element | None":
        t = self._tag.select_one(css)
        return Element(t, self._base) if t else None

    def __repr__(self):
        return f"<Element {self.text[:60]!r}>"


class Page:
    """Result of fetch(): .text (visible text), .html, .json(), .select(css), .links, .structured_data()."""

    def __init__(self, url: str, status: int, body: str, headers: dict | None = None):
        self.url, self.status, self.html, self.headers = url, status, body, headers or {}
        self._soup = None

    @property
    def soup(self):
        if self._soup is None:
            from bs4 import BeautifulSoup
            self._soup = BeautifulSoup(self.html, "html.parser")
        return self._soup

    @property
    def text(self) -> str:
        soup = self.soup
        for t in soup(["script", "style", "noscript", "template"]):
            t.decompose()
        lines = [" ".join(l.split()) for l in soup.get_text("\n").splitlines()]
        self._soup = None
        return "\n".join(l for l in lines if l)

    def json(self):
        return _json.loads(self.html)

    def select(self, css: str) -> list[Element]:
        return [Element(t, self.url) for t in self.soup.select(css)]

    def select_one(self, css: str) -> Element | None:
        t = self.soup.select_one(css)
        return Element(t, self.url) if t else None

    @property
    def links(self) -> list[dict]:
        return [{"text": " ".join(a.get_text(" ", strip=True).split()), "url": urljoin(self.url, a["href"])}
                for a in self.soup.select("a[href]")]

    def meta(self, name: str) -> str | None:
        t = self.soup.select_one(f'meta[name="{name}"], meta[property="{name}"]')
        return t.get("content") if t else None

    def structured_data(self) -> list:
        out = []
        for t in self.soup.select('script[type="application/ld+json"]'):
            try:
                d = _json.loads(t.string or "")
            except ValueError:
                continue
            out.extend(d if isinstance(d, list) else d.get("@graph", [d]) if isinstance(d, dict) else [])
        return out

    def __repr__(self):
        return f"<Page {self.status} {self.url}>"


def fetch(url: str, method: str = "GET", headers: dict | None = None, params: dict | None = None,
          json: Any = None, data: Any = None, timeout: float = 30, browser: bool = False,
          allow_error: bool = False) -> Page:
    """Fetch a web page or API. browser=True renders JavaScript with headless Chromium (slower)."""
    if browser:
        return _browser_fetch(url, timeout)
    import httpx
    fc = _tcfg("fetch")
    # Apply tool_config defaults (caller-supplied values take precedence)
    if "timeout" in fc and timeout == 30:
        try:
            timeout = float(fc["timeout"])
        except (ValueError, TypeError):
            pass
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    # Apply configured default headers first, then caller headers override
    if fc.get("default_headers") and isinstance(fc["default_headers"], dict):
        h.update(fc["default_headers"])
    h.update(headers or {})
    try:
        r = httpx.request(method, url, headers=h, params=params, json=json, data=data, timeout=timeout,
                          follow_redirects=True)
    except httpx.HTTPError as e:
        raise FetchError(f"Could not reach {url}: {e}") from None
    page = Page(str(r.url), r.status_code, r.text, dict(r.headers))
    if r.status_code >= 400 and not allow_error:
        raise FetchError(f"{url} returned HTTP {r.status_code}", r.status_code)
    return page


def _browser_fetch(url: str, timeout: float) -> Page:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise JinaiError("Browser fetching is not available on this runner") from None
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        try:
            pg = b.new_page(user_agent=UA)
            resp = pg.goto(url, timeout=timeout * 1000, wait_until="networkidle")
            return Page(pg.url, resp.status if resp else 0, pg.content())
        finally:
            b.close()


# --------------------------------------------------------------------------- AI
def _llm(payload: dict) -> dict:
    import httpx
    if not _API:
        raise JinaiError("AI is not available in this environment")
    try:
        r = httpx.post(f"{_API}/internal/llm", json=payload, headers={"Authorization": f"Bearer {_TOKEN}"},
                       timeout=180)
    except httpx.HTTPError as e:
        raise JinaiError(f"AI call failed: {e}") from None
    if r.status_code == 402:
        raise BudgetExceeded(r.json().get("detail", "AI budget for this run is used up"))
    if r.status_code >= 400:
        raise JinaiError(f"AI call failed: {r.text[:300]}")
    out = r.json()
    _emit("ai_call", cost=out.get("cost", 0))
    return out


def _strip_json(text: str):
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return _json.loads(t)
    except ValueError:
        m = re.search(r"(\{.*\}|\[.*\])", t, re.S)
        if m:
            return _json.loads(m.group(1))
        raise JinaiError(f"AI did not return valid JSON: {text[:200]}")


def ai(prompt: str, system: str | None = None, json: bool = False, max_tokens: int = 1500,
       model: str | None = None) -> Any:
    """One AI call. json=True returns parsed JSON."""
    ac = _tcfg("ai")
    # Apply tool_config defaults (caller-supplied values take precedence)
    effective_model = model or ac.get("model") or None
    default_sys = ac.get("system", "")
    sys_p = system or (default_sys.strip() if default_sys else "You are a precise assistant inside an automated agent. Answer directly.")
    if json:
        sys_p += " Respond with valid JSON only, no commentary."
    payload: dict = {"system": sys_p, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
    if effective_model:
        payload["model"] = effective_model
    out = _llm(payload)
    return _strip_json(out["text"]) if json else out["text"]


_PY_TO_JSON = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


def _schema(fn: Callable) -> dict:
    sig = inspect.signature(fn)
    props, required = {}, []
    hints = getattr(fn, "__annotations__", {})
    for name, p in sig.parameters.items():
        t = hints.get(name, str)
        if isinstance(t, str):
            t = {"str": str, "int": int, "float": float, "bool": bool, "list": list, "dict": dict}.get(t, str)
        props[name] = {"type": _PY_TO_JSON.get(t, "string")}
        if p.default is inspect.Parameter.empty:
            required.append(name)
    doc = inspect.getdoc(fn) or fn.__name__
    return {"name": fn.__name__, "description": doc[:1000],
            "input_schema": {"type": "object", "properties": props, "required": required}}


def _brief(v) -> str:
    r = repr(v)
    return r if len(r) <= 40 else r[:37] + "…"


def agent(goal: str, tools: list[Callable] = (), max_steps: int | None = None, system: str | None = None,
          context: Any = None, strategy: str = "react", reflect: bool = False) -> str:
    """Let AI decide the steps: it calls your tools (plain Python functions with docstrings) in a loop
    until the goal is done, then returns its final answer.

    strategy="react"       — default: reason + act, one tool at a time
    strategy="plan_first"  — LLM writes a full plan first, then executes step by step
    reflect=True           — after each tool call, LLM evaluates if it's on track
    """
    max_steps = max_steps or _MAX_STEPS
    fns = {f.__name__: f for f in tools}
    ac = _tcfg("ai")
    # Apply tool_config defaults for the agent loop
    default_sys = ac.get("system", "")
    sys_p = system or (default_sys.strip() + "\n\nYou are an autonomous agent." if default_sys else
                       "You are an autonomous agent running unattended. Use the tools to reach the goal, "
                       "then give a concise final answer. Do not ask questions; nobody will reply.")
    if reflect:
        sys_p += (" After each tool call, briefly evaluate whether you are on track to reach the goal. "
                  "If you've gone off course, correct your approach.")
    content = goal if context is None else f"{goal}\n\nContext:\n{_json.dumps(context, default=str)[:20000]}"

    if strategy == "plan_first":
        plan_prompt = (f"You will accomplish this goal: {goal}\n\n"
                       "Available tools: " + ", ".join(fns.keys()) + "\n\n"
                       "First, write a step-by-step plan (numbered list). Be concise.")
        plan = ai(plan_prompt, system=sys_p)
        log(f"[agent] plan:\n{plan}")
        content = f"{goal}\n\nYour plan:\n{plan}\n\nNow execute the plan step by step."

    messages = [{"role": "user", "content": content}]
    schemas = [_schema(f) for f in fns.values()]
    for _ in range(max_steps):
        out = _llm({"system": sys_p, "messages": messages, "tools": schemas, "max_tokens": 2000})
        blocks = out["content"]
        messages.append({"role": "assistant", "content": blocks})
        uses = [b for b in blocks if b.get("type") == "tool_use"]
        if not uses:
            return out["text"]
        results = []
        for u in uses:
            fn = fns.get(u["name"])
            args = u.get("input") or {}
            label = f"{u['name']}(" + ", ".join(f"{k}={_brief(v)}" for k, v in args.items()) + ")"
            with step(label) as s:
                try:
                    if fn is None:
                        raise JinaiError(f"unknown tool {u['name']}")
                    res = fn(**args)
                    text = res if isinstance(res, str) else _json.dumps(res, default=str)
                    is_error = False
                except Exception as e:
                    text, is_error = f"Error: {type(e).__name__}: {e}", True
                    s.note(text)
            results.append({"type": "tool_result", "tool_use_id": u["id"], "content": text[:12000],
                            "is_error": is_error})
        messages.append({"role": "user", "content": results})
    raise JinaiError(f"Agent did not finish within {max_steps} steps")


# --------------------------------------------------------------------------- helpers
def _read_json(p: Path):
    try:
        return _json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except ValueError:
        return None


def _finish(output: Any):
    state._save()
    (_IO / "result.json").write_text(_json.dumps({"output": output}, default=str), encoding="utf-8")


def _fail(exc: BaseException, tb: str):
    state._save()
    (_IO / "error.json").write_text(_json.dumps(
        {"type": type(exc).__name__, "message": str(exc)[:2000], "traceback": tb[-6000:]}), encoding="utf-8")
    print(tb, file=sys.stderr)
