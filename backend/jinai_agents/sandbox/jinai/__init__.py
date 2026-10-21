"""jinai: the helper library available to every agent.

    from jinai import step, check, fetch, state, ai, agent, notify, secret, prompt, tools

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
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

__all__ = ["step", "check", "fetch", "state", "ai", "agent", "notify", "secret", "prompt", "tools", "log",
           "Page", "JinaiError", "MissingSecret", "CheckFailed", "BudgetExceeded", "FetchError"]

_IO = Path(os.environ.get("JINAI_IO", "."))
_API = os.environ.get("JINAI_API", "").rstrip("/")
_TOKEN = os.environ.get("JINAI_RUN_TOKEN", "")
_MAX_STEPS = int(os.environ.get("JINAI_MAX_STEPS", "20"))
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0 Safari/537.36 JinaiAgent/0.1")


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


class step:
    """Mark a stage of the agent. Shown in the Steps view and in each run's detail.

        with step("Read leads sheet") as s:
            rows = ...
            s.note(f"{len(rows)} rows")
    """

    def __init__(self, name: str):
        self.name = str(name)
        self.id = None

    def __enter__(self):
        _counter[0] += 1
        self.id = _counter[0]
        parent = _stack[-1].id if _stack else None
        _stack.append(self)
        _emit("step_start", id=self.id, name=self.name, parent=parent)
        return self

    def note(self, text: Any):
        _emit("step_note", id=self.id, text=str(text)[:500])

    def __exit__(self, exc_type, exc, tb):
        if _stack and _stack[-1] is self:
            _stack.pop()
        if exc is None:
            _emit("step_end", id=self.id, status="ok")
        else:
            _emit("step_end", id=self.id, status="error", error=f"{exc_type.__name__}: {exc}"[:1000])
        return False

    def __call__(self, func):
        @functools.wraps(func)
        def wrapper(*a, **kw):
            with step(self.name):
                return func(*a, **kw)
        return wrapper


def check(condition: Any, message: str, stop: bool = False) -> bool:
    """Sanity-check a result. A failed check marks the run 'suspicious': its notifications are held
    back and the repair loop looks at it. With stop=True the run also stops here."""
    ok = bool(condition)
    _emit("check", ok=ok, message=str(message)[:300], step=_stack[-1].name if _stack else None)
    if not ok and stop:
        raise CheckFailed(message)
    return ok


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


# --------------------------------------------------------------------------- secrets, prompts, notify
def secret(name: str) -> str:
    """Read a secret the owner added in Settings → Secrets (API keys, passwords)."""
    v = os.environ.get("JINAI_SECRET_" + name)
    if not v:
        raise MissingSecret(f"Secret '{name}' is not set. Add it in Settings → Secrets.")
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
    _emit("notify", message=str(message)[:20000], subject=(subject or "")[:200], to=to or "")


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
        self._soup = None  # decompose mutated it
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
        """JSON-LD blocks (schema.org Product, Offer, Article...). Often the most stable place for prices."""
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
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
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


def ai(prompt: str, system: str | None = None, json: bool = False, max_tokens: int = 1500) -> Any:
    """One AI call. json=True returns parsed JSON."""
    sys_p = system or "You are a precise assistant inside an automated agent. Answer directly."
    if json:
        sys_p += " Respond with valid JSON only, no commentary."
    out = _llm({"system": sys_p, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens})
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
          context: Any = None) -> str:
    """Let AI decide the steps: it calls your tools (plain Python functions with docstrings) in a loop
    until the goal is done, then returns its final answer."""
    max_steps = max_steps or _MAX_STEPS
    fns = {f.__name__: f for f in tools}
    sys_p = system or ("You are an autonomous agent running unattended. Use the tools to reach the goal, "
                       "then give a concise final answer. Do not ask questions; nobody will reply.")
    content = goal if context is None else f"{goal}\n\nContext:\n{_json.dumps(context, default=str)[:20000]}"
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
                except Exception as e:  # tool errors go back to the model, not up the stack
                    text, is_error = f"Error: {type(e).__name__}: {e}", True
                    s.note(text)
            results.append({"type": "tool_result", "tool_use_id": u["id"], "content": text[:12000],
                            "is_error": is_error})
        messages.append({"role": "user", "content": results})
    raise JinaiError(f"Agent did not finish within {max_steps} steps")


# --------------------------------------------------------------------------- ready-made tools for agent()
class tools:
    @staticmethod
    def fetch_page(url: str) -> str:
        """Fetch a web page and return its visible text (first 8000 characters)."""
        return fetch(url).text[:8000]

    @staticmethod
    def fetch_links(url: str) -> list:
        """Fetch a web page and return its links as {text, url}."""
        return fetch(url).links[:200]

    @staticmethod
    def fetch_json(url: str) -> str:
        """GET a JSON API URL and return the raw JSON text (first 12000 characters)."""
        return fetch(url, headers={"Accept": "application/json"}).html[:12000]


def _finish(output: Any):
    state._save()
    (_IO / "result.json").write_text(_json.dumps({"output": output}, default=str), encoding="utf-8")


def _fail(exc: BaseException, tb: str):
    state._save()
    (_IO / "error.json").write_text(_json.dumps(
        {"type": type(exc).__name__, "message": str(exc)[:2000], "traceback": tb[-6000:]}), encoding="utf-8")
    print(tb, file=sys.stderr)
