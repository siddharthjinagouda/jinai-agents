"""Demo mode: runs Jinai with a scripted stand-in for Claude and a small fake website, so you can click
through the product without an API key. Data goes to ./demo-data (deleted on each start).

    python scripts/demo.py            # then open http://127.0.0.1:8300  (token: demo)
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "demo-data"
shutil.rmtree(DATA, ignore_errors=True)
DATA.mkdir()
SITE_PORT, PORT = 8399, int(os.environ.get("PORT", "8300"))
SITE = f"http://127.0.0.1:{SITE_PORT}"
os.environ.update({
    "JINAI_DATABASE_URL": f"sqlite:///{DATA}/jinai.db", "JINAI_DATA_DIR": str(DATA / "runs"),
    "JINAI_ADMIN_TOKEN": "demo", "JINAI_INTERNAL_URL": f"http://127.0.0.1:{PORT}",
    "JINAI_PUBLIC_URL": f"http://127.0.0.1:{PORT}", "JINAI_OWNER_EMAIL": "you@example.com", "JINAI_SCHEDULER": "1",
})
sys.path.insert(0, str(ROOT))

from jinai_agents import config  # noqa: E402

config.reload()
from jinai_agents import db, llm, runner  # noqa: E402
from jinai_agents.db import Agent, session  # noqa: E402
from jinai_agents.store import deploy  # noqa: E402

# ------------------------------------------------------------------ fake website
PAGES = {
    "/notion/pricing": '<div class="plan" data-plan="plus"><h3>Plus</h3><span class="price">$10</span></div>'
                       '<div class="plan" data-plan="business"><h3>Business</h3><span class="price">$15</span></div>',
    "/coda/pricing": '<div class="plan" data-plan="plus"><h3>Pro</h3><span class="price">$12</span></div>'
                     '<div class="plan" data-plan="business"><h3>Team</h3><span class="price">$36</span></div>',
    "/status": json.dumps({"api": "operational", "dashboard": "operational", "latency_ms": 182}),
}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path)
        self.send_response(200 if body else 404)
        self.end_headers()
        self.wfile.write((body or "not found").encode())

    def log_message(self, *a):
        pass


threading.Thread(target=http.server.ThreadingHTTPServer(("127.0.0.1", SITE_PORT), H).serve_forever, daemon=True).start()

# ------------------------------------------------------------------ agents
PRICING = f'''from jinai import step, check, fetch, state, notify, ai, prompt

COMPETITORS = {{
    "Notion": "{SITE}/notion/pricing",
    "Coda": "{SITE}/coda/pricing",
}}


def read_prices(url):
    page = fetch(url)
    prices = {{}}
    for plan in page.select(".plan"):
        name = plan.select_one("h3").text
        prices[name] = plan.select_one(".price").text
    return prices


def run(event):
    current = {{}}
    with step("Read competitor pricing pages") as s:
        for name, url in COMPETITORS.items():
            current[name] = read_prices(url)
        s.note(", ".join(f"{{n}}: {{len(p)}} plans" for n, p in current.items()))
        check(all(current.values()), "every page has at least one plan")
        check(all(p.startswith("$") for c in current.values() for p in c.values()), "prices look like dollar amounts")

    with step("Compare with last week") as s:
        last = state.get("prices")
        state.set("prices", current)
        if last is None:
            s.note("first run: saved as baseline")
            return {{"changed": False}}
        changes = {{n: (last.get(n), current[n]) for n in current if last.get(n) != current[n]}}
        s.note(f"{{len(changes)}} competitor(s) changed")

    if changes:
        with step("Write the summary"):
            summary = ai(prompt("summary", changes=changes))
        with step("Email the summary"):
            notify(summary, subject="Competitor pricing changed")
    return {{"changed": bool(changes)}}
'''

STATUS = f'''from jinai import step, check, fetch, state, notify

URL = "{SITE}/status"


def run(event):
    with step("Read status page") as s:
        data = fetch(URL).json()
        check("api" in data, "status page lists the API")
        s.note(f"API {{data['api']}}, {{data['latency_ms']}} ms")
    with step("Alert if something is down"):
        down = [k for k, v in data.items() if isinstance(v, str) and v != "operational"]
        if down and down != state.get("down"):
            notify(f"Down: {{', '.join(down)}}", subject="Status alert")
        state.set("down", down)
    return {{"down": down}}
'''

TRIAGE = '''from jinai import step, ai, check, notify, prompt


def run(event):
    with step("Read the ticket") as s:
        text = event.get("text", "")
        check(len(text) > 0, "ticket has text", stop=True)
        s.note(text[:80])
    with step("Classify urgency and topic") as s:
        label = ai(prompt("classify", ticket=text), json=True)
        s.note(f"{label['topic']}, {label['urgency']}")
    if label["urgency"] == "high":
        with step("Alert on-call"):
            notify(f"Urgent ticket: {text[:200]}", subject="Urgent support ticket")
    return label
'''


def fake_claude(system, messages, tools):
    if tools and any(t["name"] == "write_file" for t in tools):
        return {"content": [{"type": "text", "text": "Demo mode: connect Claude (ANTHROPIC_API_KEY) to build "
                                                     "and change agents from chat."}]}
    last = messages[-1]["content"]
    if "classify" in str(last).lower() or "ticket" in str(last).lower():
        return {"content": [{"type": "text", "text": '{"topic": "billing", "urgency": "high"}'}]}
    return {"content": [{"type": "text", "text": "Coda raised its Pro plan from $12 to $14 per user. Notion is "
                                                 "unchanged. Coda is now $4 more than Notion Plus."}]}


llm.set_llm(llm.ScriptedLLM(fake_claude))


def make(name, desc, files, trigger, schedule="", tz="UTC"):
    with session() as s:
        a = Agent(name=name, description=desc, draft_files=files, trigger_type=trigger, schedule=schedule,
                  timezone=tz, alert_email="you@example.com", needed_secrets=[])
        s.add(a)
        s.flush()
        deploy(s, a, files, "first version", "builder")
        s.add(db.Message(agent_id=a.id, role="user", text=desc))
        s.add(db.Message(agent_id=a.id, role="assistant", text=f"Here's {name}. Review the steps, then deploy."))
        return a.id


def wait(run_id):
    while True:
        with session() as s:
            if s.get(db.Run, run_id).status not in ("queued", "running"):
                return
        time.sleep(0.2)


def seed():
    time.sleep(1.5)
    pricing = make("Competitor pricing watch", "Every Monday, check Notion and Coda pricing and email me what changed",
                   {"agent.py": PRICING, "prompts/summary.md": "Summarise these pricing changes for a founder in 2-3 "
                    "sentences. Mention plan names and old/new prices.\n\n{{changes}}\n"},
                   "schedule", "0 9 * * 1", "Asia/Kolkata")
    status = make("API status monitor", "Every 15 minutes, check our status page and alert me if anything is down",
                  {"agent.py": STATUS}, "schedule", "*/15 * * * *")
    triage = make("Support ticket triage", "When a new ticket arrives, classify it and alert me if it's urgent",
                  {"agent.py": TRIAGE, "prompts/classify.md": 'Classify this support ticket. Return JSON '
                   '{"topic": billing|bug|question|other, "urgency": low|medium|high}.\n\n{{ticket}}\n'}, "webhook")
    for _ in range(6):
        wait(runner.queue_run(status, "schedule"))
    wait(runner.queue_run(pricing, "schedule"))
    PAGES["/coda/pricing"] = PAGES["/coda/pricing"].replace("$12", "$14")
    wait(runner.queue_run(pricing, "schedule"))
    for t in ("I was charged twice this month", "How do I export to PDF?"):
        wait(runner.queue_run(triage, "webhook", {"text": t}))
    print(f"\nDemo ready: http://127.0.0.1:{PORT}  (access token: demo)\n", flush=True)


if __name__ == "__main__":
    import uvicorn
    threading.Thread(target=seed, daemon=True).start()
    uvicorn.run("jinai_agents.api:app", host="127.0.0.1", port=PORT, log_level="warning")
