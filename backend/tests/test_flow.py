"""End-to-end: build → test → deploy → run → change detected → site redesign → auto-repair → unfixable → paused."""
from __future__ import annotations

import time

from conftest import SITE, Script, Site, last_tool_result, text, tool_use, wait_for

from jinai_agents import notify, scheduler
from jinai_agents.db import Agent, session

PRICE_AGENT = '''from jinai import step, check, fetch, state, notify

URL = "{site}/pricing"


def run(event):
    with step("Read pricing page") as s:
        page = fetch(URL)
        el = page.select_one(".plan-plus .price")
        check(el is not None, "found the Plus price on the page", stop=True)
        price = el.text
        check(price.startswith("$"), "price looks like a dollar amount")
        s.note(f"Plus price: {{price}}")
    with step("Compare with last time") as s:
        last = state.get("price")
        state.set("price", price)
        if last is None:
            s.note("first run: saved baseline")
            return {{"price": price, "changed": False}}
        changed = last != price
        s.note("changed" if changed else "no change")
    if changed:
        with step("Send alert"):
            notify(f"Plus plan price changed from {{last}} to {{price}}", subject="Pricing changed")
    return {{"price": price, "changed": changed}}
'''.format(site=SITE)


def page_v1(price="$10"):
    return f'<html><body><div class="plan-plus"><h2>Plus</h2><span class="price">{price}</span></div></body></html>'


def page_redesigned(price="$12"):
    return f'<html><body><section data-plan="plus"><h3>Plus</h3><b class="amount">{price}</b></section></body></html>'


def build_script():
    """What Claude does for 'watch the Plus price': write code, set schedule, test, reply."""
    def after_test(messages):
        res = last_tool_result(messages)
        assert res["status"] == "success", res
        return [text("Here's your agent:\n1. Read the pricing page\n2. Compare with last time\n3. Email you if the "
                     f"Plus price changes\nRuns daily at 09:00. Test run: Plus is {res['output']['price']}.")]
    return [
        [text("I'll write the agent."), tool_use("write_file", path="agent.py", content=PRICE_AGENT)],
        [tool_use("update_settings", name="Plus price watch", trigger_type="schedule", schedule="0 9 * * *",
                  timezone="Asia/Kolkata")],
        [tool_use("test_run")],
        after_test,
    ]


def create_and_build(api, request="Tell me when the Plus price on the pricing page changes"):
    Script.build = build_script()
    agent_id = api.post("/api/agents", json={"request": request}).json()["id"]
    wait_for(lambda: not api.get(f"/api/agents/{agent_id}").json()["builder_busy"])
    return agent_id


def test_auth_required(server):
    import httpx
    assert httpx.get(f"{server}/api/agents").status_code == 401
    assert httpx.get(f"{server}/api/agents", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_full_lifecycle(api):
    Site.routes["/pricing"] = (200, page_v1("$10"))
    agent_id = create_and_build(api)
    d = api.get(f"/api/agents/{agent_id}").json()

    # built, tested, not yet deployed
    assert d["name"] == "Plus price watch" and d["status"] == "draft"
    assert d["trigger"] == "Daily 09:00 (Asia/Kolkata)"
    assert [s["name"] for s in d["outline"]] == ["Read pricing page", "Compare with last time", "Send alert"]
    assert d["outline"][0]["checks"] == 2 and d["outline"][2]["notifies"]
    msgs = api.get(f"/api/agents/{agent_id}/messages").json()
    assert msgs[0]["role"] == "user" and msgs[-1]["role"] == "assistant"
    assert "Plus is $10" in msgs[-1]["text"]
    assert any("Test run ✓ passed" in m["text"] for m in msgs if m["role"] == "activity")

    # deploy → schedule set
    r = api.post(f"/api/agents/{agent_id}/deploy", json={"note": "first"}).json()
    assert r["version"] == 1 and r["next_run_at"]
    d = api.get(f"/api/agents/{agent_id}").json()
    assert d["status"] == "live" and d["live_version"] == 1 and not d["draft_dirty"]

    # first run: baseline, nothing sent
    run1 = api.post(f"/api/agents/{agent_id}/run", json={}).json()["run_id"]
    r1 = wait_for(lambda: (x := api.get(f"/api/runs/{run1}").json())["status"] not in ("queued", "running") and x)
    assert r1["status"] == "success", r1
    assert r1["output"] == {"price": "$10", "changed": False}
    assert api.get(f"/api/agents/{agent_id}").json()["memory"] == {"price": "$10"}

    # price changes → alert sent to owner
    Site.routes["/pricing"] = (200, page_v1("$12"))
    before = len(notify.outbox)
    run2 = api.post(f"/api/agents/{agent_id}/run", json={}).json()["run_id"]
    r2 = wait_for(lambda: (x := api.get(f"/api/runs/{run2}").json())["status"] not in ("queued", "running") and x)
    assert r2["status"] == "success" and r2["output"]["changed"] is True
    assert r2["notifications"][0]["status"].startswith("kept")  # no SMTP in tests
    assert notify.outbox[before]["to"] == "owner@example.com"
    assert "from $10 to $12" in notify.outbox[before]["body"]
    assert [s["name"] for s in r2["steps"]] == ["Read pricing page", "Compare with last time", "Send alert"]

    # site redesign → run fails → auto-repair edits selector, tests, deploys v2, re-runs
    Site.routes["/pricing"] = (200, page_redesigned("$12"))

    def repair_test(messages):
        return [tool_use("edit_file", path="agent.py", old='page.select_one(".plan-plus .price")',
                         new='page.select_one("[data-plan=plus] .amount")')]

    def repair_done(messages):
        res = last_tool_result(messages)
        assert res["status"] == "success", res
        return [text("The pricing page was redesigned; the price moved to a new element.\n"
                     "FIXED: updated the price selector to the new page layout.")]
    Script.repair = [repair_test, [tool_use("test_run")], repair_done]
    run3 = api.post(f"/api/agents/{agent_id}/run", json={}).json()["run_id"]
    wait_for(lambda: api.get(f"/api/runs/{run3}").json()["repair_status"] == "fixed", timeout=40)
    r3 = api.get(f"/api/runs/{run3}").json()
    assert r3["status"] == "failed" and "found the Plus price" in r3["error"]
    d = api.get(f"/api/agents/{agent_id}").json()
    assert d["live_version"] == 2 and d["status"] == "live"
    assert d["versions"][0]["by"] == "repair" and "selector" in d["versions"][0]["note"]
    assert "[data-plan=plus] .amount" in d["files"]["agent.py"]  # draft follows the fix
    events = api.get("/api/events").json()
    assert events[0]["kind"] == "fixed"
    # the missed run is redone on the fixed version
    runs = wait_for(lambda: [r for r in api.get(f"/api/agents/{agent_id}/runs").json()
                             if r["version"] == 2 and r["status"] == "success"])
    assert runs

    # site starts blocking bots → can't be fixed by code → paused + owner alerted
    Site.routes["/pricing"] = (403, "Forbidden")
    Script.repair = [[text("The site now returns HTTP 403 to automated requests. Changing the code won't help.\n"
                           "NOT FIXED: the pricing page now blocks automated access (HTTP 403).")]]
    before = len(notify.outbox)
    run4 = api.post(f"/api/agents/{agent_id}/run", json={}).json()["run_id"]
    wait_for(lambda: api.get(f"/api/runs/{run4}").json()["repair_status"] == "not_fixed", timeout=40)
    d = api.get(f"/api/agents/{agent_id}").json()
    assert d["status"] == "needs_attention"
    alert = notify.outbox[before]
    assert "needs your attention" in alert["subject"] and "HTTP 403" in alert["body"]
    summary = api.get("/api/summary").json()
    assert summary["needs_you"] >= 1 and summary["fixed_7d"] >= 1
    # scheduled runs are off while paused; resume turns them back on
    assert d["next_run_at"] is None
    api.post(f"/api/agents/{agent_id}/resume")
    assert api.get(f"/api/agents/{agent_id}").json()["next_run_at"]

    # rollback to v1
    v = api.post(f"/api/agents/{agent_id}/rollback", json={"number": 1}).json()["version"]
    d = api.get(f"/api/agents/{agent_id}").json()
    assert d["live_version"] == v == 3 and ".plan-plus .price" in d["files"]["agent.py"]


def test_suspicious_run_holds_notifications(api):
    code = '''from jinai import step, check, notify
def run(event):
    with step("Compute"):
        value = -5
        check(value > 0, "value is positive")
        notify(f"value is {value}")
    return {"value": value}
'''
    with session() as s:
        a = Agent(name="sus", draft_files={"agent.py": code}, auto_repair=False)
        s.add(a)
        s.flush()
        aid = a.id
    api.post(f"/api/agents/{aid}/deploy", json={})
    run_id = api.post(f"/api/agents/{aid}/run", json={}).json()["run_id"]
    r = wait_for(lambda: (x := api.get(f"/api/runs/{run_id}").json())["status"] not in ("queued", "running") and x)
    assert r["status"] == "suspicious"
    assert r["notifications"][0]["status"] == "held (run suspicious)"
    assert r["summary"].startswith("check failed")


def test_user_edits_code_and_test_run(api):
    with session() as s:
        a = Agent(name="edit me")
        s.add(a)
        s.flush()
        aid = a.id
    code = 'from jinai import step, state\ndef run(event):\n    with step("Count") as s:\n' \
           '        n = state.get("n", 0) + 1\n        state.set("n", n)\n        s.note(f"n={n}")\n    return n\n'
    r = api.put(f"/api/agents/{aid}/files", json={"files": {"agent.py": code}}).json()
    assert r["outline"][0]["name"] == "Count" and r["syntax_errors"] == []
    t = api.post(f"/api/agents/{aid}/test", json={}).json()
    assert t["status"] == "success" and t["output"] == 1
    assert api.get(f"/api/agents/{aid}").json()["memory"] == {}  # tests don't save memory
    bad = api.put(f"/api/agents/{aid}/files", json={"files": {"agent.py": "def run(event)\n  pass"}}).json()
    assert bad["syntax_errors"][0]["line"] == 1
    assert api.post(f"/api/agents/{aid}/test", json={}).status_code == 400
    assert api.post(f"/api/agents/{aid}/deploy", json={}).status_code == 400
    assert api.put(f"/api/agents/{aid}/files", json={"files": {"../x.py": "x"}}).status_code == 400
    assert api.put(f"/api/agents/{aid}/files", json={"files": {"run.sh": "x"}}).status_code == 400


def test_webhook_with_ai_and_agent_loop(api):
    code = '''from jinai import step, ai, agent, check

def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

def run(event):
    with step("Classify message") as s:
        label = ai(f"Classify: {event['text']}", json=True)["label"]
        s.note(label)
    with step("Work it out"):
        answer = agent("What is 2 + 3? Use the tool.", tools=[add])
    check(answer.strip() == "5", "agent answered 5")
    return {"label": label, "answer": answer}
'''

    def runtime(system, messages, tools):
        if tools:  # agent() loop
            res = last_tool_result(messages)
            if res is None:
                return [tool_use("add", a=2, b=3)]
            return [text(str(res))]
        return [text('```json\n{"label": "billing"}\n```')]
    Script.runtime = runtime
    with session() as s:
        a = Agent(name="hook", trigger_type="webhook", draft_files={"agent.py": code})
        s.add(a)
        s.flush()
        aid, secret = a.id, a.webhook_secret
    api.post(f"/api/agents/{aid}/deploy", json={})
    import httpx
    url = api.get(f"/api/agents/{aid}").json()["webhook_url"]
    assert url.endswith(f"/hooks/{aid}/{secret}")
    assert httpx.post(url.replace(secret, "wrong"), json={"text": "x"}).status_code == 404
    r = httpx.post(url + "?wait=30", json={"text": "I was charged twice"}, timeout=60).json()
    assert r["status"] == "success", api.get(f"/api/runs/{r['run_id']}").json()
    assert r["output"] == {"label": "billing", "answer": "5"}
    run = api.get(f"/api/runs/{r['run_id']}").json()
    assert run["ai_calls"] == 3 and run["ai_cost"] > 0
    names = [s["name"] for s in run["steps"]]
    assert names == ["Classify message", "Work it out", "add(a=2, b=3)"]
    assert run["steps"][2]["depth"] == 1


def test_budget_limit_stops_ai(api):
    code = 'from jinai import ai\ndef run(event):\n    for i in range(50):\n        ai("hi")\n'
    with session() as s:
        a = Agent(name="spender", draft_files={"agent.py": code}, max_cost_per_run=0.02, auto_repair=False)
        s.add(a)
        s.flush()
        aid = a.id
    t = api.post(f"/api/agents/{aid}/test", json={}).json()
    assert t["status"] == "failed" and "BudgetExceeded" in t["error"]
    assert t["ai_calls"] == 4  # $0.006 per scripted call; the 5th would exceed $0.02


def test_timeout_and_secrets(api):
    code = 'import time\nfrom jinai import secret\ndef run(event):\n    k = secret("API_KEY")\n' \
           '    if event.get("sleep"):\n        time.sleep(30)\n    return k[:3]\n'
    with session() as s:
        a = Agent(name="slow", draft_files={"agent.py": code}, timeout_s=2, auto_repair=False)
        s.add(a)
        s.flush()
        aid = a.id
    t = api.post(f"/api/agents/{aid}/test", json={}).json()
    assert t["status"] == "failed" and "MissingSecret" in t["error"]
    api.put(f"/api/agents/{aid}/secrets", json={"name": "api_key", "value": "sk-secret-123"})
    d = api.get(f"/api/agents/{aid}").json()
    assert d["secrets_set"] == ["API_KEY"] and "sk-secret" not in str(d)
    t = api.post(f"/api/agents/{aid}/test", json={}).json()
    assert t["status"] == "success" and t["output"] == "sk-"
    start = time.time()
    t = api.post(f"/api/agents/{aid}/test", json={"event": {"sleep": True}}).json()
    assert t["status"] == "failed" and "Timed out" in t["error"] and time.time() - start < 15


def test_scheduler_runs_due_agents(api):
    code = 'from jinai import step\ndef run(event):\n    with step("Tick"):\n        pass\n    return "tick"\n'
    with session() as s:
        a = Agent(name="sched", trigger_type="schedule", schedule="*/5 * * * *", draft_files={"agent.py": code})
        s.add(a)
        s.flush()
        aid = a.id
    api.post(f"/api/agents/{aid}/deploy", json={})
    from datetime import timedelta

    from jinai_agents.db import as_utc, now
    with session() as s:
        s.get(Agent, aid).next_run_at = now() - timedelta(seconds=1)
    assert aid in scheduler.tick()
    runs = wait_for(lambda: [r for r in api.get(f"/api/agents/{aid}/runs").json() if r["status"] == "success"])
    assert runs[0]["kind"] == "schedule"
    with session() as s:
        assert as_utc(s.get(Agent, aid).next_run_at) > now()


def test_three_failures_pause_without_repair(api):
    code = 'def run(event):\n    raise RuntimeError("boom")\n'
    with session() as s:
        a = Agent(name="broken", draft_files={"agent.py": code}, auto_repair=False)
        s.add(a)
        s.flush()
        aid = a.id
    api.post(f"/api/agents/{aid}/deploy", json={})
    for _ in range(3):
        rid = api.post(f"/api/agents/{aid}/run", json={}).json()["run_id"]
        wait_for(lambda: api.get(f"/api/runs/{rid}").json()["status"] == "failed")
    d = wait_for(lambda: (x := api.get(f"/api/agents/{aid}").json())["status"] == "needs_attention" and x)
    assert d["status"] == "needs_attention"


def test_builder_handles_chat_followup(api):
    """Second message edits existing code; Claude sees the current files in context."""
    Site.routes["/pricing"] = (200, page_v1("$10"))
    agent_id = create_and_build(api)

    def check_context(messages):
        ctx = messages[-1]["content"]
        assert "### agent.py" in ctx and "Also mention the old price" in ctx
        assert messages[0]["role"] == "user" and "Tell me when the Plus price" in messages[0]["content"]
        return [tool_use("edit_file", path="agent.py", old='subject="Pricing changed"', new='subject="Plus price changed"')]
    Script.build = [check_context, [tool_use("test_run")], [text("Updated the email subject.")]]
    api.post(f"/api/agents/{agent_id}/messages", json={"text": "Also mention the old price in the subject"})
    wait_for(lambda: not api.get(f"/api/agents/{agent_id}").json()["builder_busy"])
    d = api.get(f"/api/agents/{agent_id}").json()
    assert 'subject="Plus price changed"' in d["files"]["agent.py"] and d["draft_dirty"]
    assert api.get(f"/api/agents/{agent_id}/messages").json()[-1]["text"] == "Updated the email subject."


def test_docker_command_is_locked_down(tmp_path):
    from jinai_agents.runner import docker_command
    cmd = " ".join(docker_command("run_x", tmp_path / "a", tmp_path / "io", {"JINAI_RUN_TOKEN": "t"}))
    for flag in ("--read-only", "--cap-drop ALL", "no-new-privileges", "--pids-limit 256", "--memory 768m",
                 ":/agent:ro", ":/jinai:ro", "python -I /jinai/boot.py /agent"):
        assert flag in cmd
