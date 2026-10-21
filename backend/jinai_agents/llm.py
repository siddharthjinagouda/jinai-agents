"""Claude API client (Messages API over HTTP). Tests swap in a scripted model with set_llm()."""
from __future__ import annotations

from typing import Callable

import httpx

from . import config


class LLMError(Exception):
    pass


def cost_of(usage: dict) -> float:
    s = config.settings
    return (usage.get("input_tokens", 0) * s.price_in + usage.get("output_tokens", 0) * s.price_out) / 1_000_000


def text_of(resp: dict) -> str:
    return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()


class AnthropicLLM:
    def __init__(self, api_key: str | None = None, transport: httpx.BaseTransport | None = None):
        self.api_key = api_key or config.settings.anthropic_api_key
        self.client = httpx.Client(timeout=300, transport=transport)

    def create(self, *, model: str, system: str, messages: list, tools: list | None = None,
               max_tokens: int = 4096) -> dict:
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        body = {"model": model, "system": system, "messages": messages, "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
        last = None
        for attempt in range(3):
            try:
                r = self.client.post("https://api.anthropic.com/v1/messages", json=body, headers={
                    "x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
            except httpx.HTTPError as e:
                last = str(e)
                continue
            if r.status_code in (429, 500, 502, 503, 529):
                last = r.text[:300]
                import time
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise LLMError(f"Claude API error {r.status_code}: {r.text[:500]}")
            return r.json()
        raise LLMError(f"Claude API unavailable: {last}")


class ScriptedLLM:
    """For tests and demos: `fn(system, messages, tools) -> response dict`."""

    def __init__(self, fn: Callable):
        self.fn = fn
        self.calls = 0

    def create(self, *, model, system, messages, tools=None, max_tokens=4096):
        self.calls += 1
        resp = self.fn(system, messages, tools)
        resp.setdefault("usage", {"input_tokens": 1000, "output_tokens": 200})
        resp.setdefault("stop_reason", "tool_use" if any(b.get("type") == "tool_use" for b in resp["content"])
                        else "end_turn")
        return resp


_llm = None


def get_llm():
    global _llm
    if _llm is None:
        _llm = AnthropicLLM()
    return _llm


def set_llm(llm):
    global _llm
    _llm = llm
