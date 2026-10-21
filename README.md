# Jinai Agents

Describe an agent in plain words. Claude builds it as real Python code you can read and edit. Jinai runs it
on a schedule or webhook, checks its results, and fixes it when it breaks.

```
 You: "Every Monday, check Notion and Coda pricing and email me what changed"
   │
   ▼
 Builder (Claude + tools)  plan → write code → test run → fix until it passes → you review → Deploy
   │
   ▼
 Runner   trigger fires → sandboxed run → steps, checks, messages recorded → messages sent if checks pass
   │ run failed or a check failed
   ▼
 Repair   Claude reads the failed run → fixes → re-tests → deploys a new version → redoes the run
          (or pauses the agent and emails you what happened and why it can't fix it)
```

## What's in v0.1

| Part | What it does |
|---|---|
| **Web app** | Home (all agents, a run strip per agent, what happened); agent page with **Chat**, **Steps / Code / Settings** and **Runs**; run detail |
| **Builder** | Claude in a tool loop: reads and writes files, sets the trigger, runs tests, reads past runs, explains in plain words |
| **Steps view** | Generated from the code (each `step("…")`), with tags for AI use, checks and messages; click a step to jump to its code |
| **Code editor** | All files (agent.py, prompts/*.md), autosave, syntax errors, versions and rollback |
| **Triggers** | Schedule (cron + time zone), webhook (with `?wait=30` to get the result back), manual |
| **Runner** | One run at a time per agent, time limit, AI budget per run, secrets as encrypted values; `process` (local) or `docker` (locked-down container) |
| **Checks** | `check()` in the code; a failed check marks the run "suspicious" and **holds its messages** so wrong data isn't sent |
| **Repair** | On a failed or suspicious run: Claude repairs and redeploys (max 3 a day per agent), or pauses the agent and emails you |
| **Memory** | `state` persists between successful runs; visible and clearable in the UI |
| **Cost** | Every AI call is metered (builder, repair, runs) and shown per run and per month |

## The `jinai` library (what agents are written with)

```python
from jinai import step, check, fetch, state, ai, agent, notify, secret, prompt, tools

def run(event):                                   # event = webhook body, or {} for schedule/manual
    with step("Read pricing page") as s:          # shown in Steps and in each run
        page = fetch("https://example.com/pricing")        # .text .select(css) .json() .structured_data()
        price = page.select_one(".plan-plus .price").text
        check(price.startswith("$"), "price looks right")  # failing => messages held, repair looks at it
        s.note(price)
    if price != state.get("price"):               # memory between runs
        notify(f"Price is now {price}")           # email after the run, only if checks passed
        state.set("price", price)
    summary = ai(prompt("summary", price=price))  # one Claude call; prompts/summary.md is editable text
    answer = agent("Research this company", tools=[tools.fetch_page])   # AI decides the steps
    return {"price": price}
```

## Run it locally

```bash
cd web && npm install && npm run build && cd ..          # builds the web app into web/out
cd backend && pip install -e .
python scripts/demo.py                                    # demo: fake website + stand-in for Claude, no key needed
# open http://127.0.0.1:8300, access token: demo
```

With Claude:
```bash
export ANTHROPIC_API_KEY=sk-ant-... JINAI_ADMIN_TOKEN=pick-a-token JINAI_RUNNER=process
jinai-agents serve --port 8300
```

Tests (11, covering build → test → deploy → run → change detected → redesign → auto-repair → unfixable →
paused, plus webhooks, AI budget, timeouts, secrets, scheduler, checks):
```bash
cd backend && pip install pytest && pytest -q
```
They pass on SQLite and PostgreSQL (`TEST_DATABASE_URL=postgresql+psycopg2://…`).

## Deploy on your GCP server

1. **Server:** e2-standard-2 or larger. Install Docker, PostgreSQL, nginx, Python 3.10+ (Node 20+ only if you change the web app).
2. **Database:** `CREATE ROLE jinai_agents LOGIN PASSWORD '…'; CREATE DATABASE jinai_agents OWNER jinai_agents;`
3. **Code:**
   ```bash
   sudo useradd --system --create-home jinai && sudo usermod -aG docker jinai
   sudo mkdir -p /opt/jinai-agents /var/lib/jinai-agents && sudo cp -r . /opt/jinai-agents
   sudo chown -R jinai /opt/jinai-agents /var/lib/jinai-agents
   cd /opt/jinai-agents/web && npm ci && npm run build
   python3 -m venv /opt/jinai-agents/venv && /opt/jinai-agents/venv/bin/pip install /opt/jinai-agents/backend
   docker build -t jinai-runner:latest -f /opt/jinai-agents/deploy/runner.Dockerfile /opt/jinai-agents/deploy
   ```
   The package installs to the venv, so also set `JINAI_WEB_DIR=/opt/jinai-agents/web/out`.
4. **Settings:** copy `backend/.env.example` to `/etc/jinai-agents.env` (mode 600) and fill it in.
5. **Service:** `sudo cp deploy/jinai-agents.service /etc/systemd/system/ && sudo systemctl enable --now jinai-agents`
6. **HTTPS:** `deploy/nginx.conf` → `/etc/nginx/sites-enabled/`, point DNS at the server, `sudo certbot --nginx`.
   Keep port 8300 closed in the GCP firewall.

## Honest limits of v0.1

- **Claude was simulated in testing.** This workspace has no API key, so the builder and repair loops were
  tested with a scripted stand-in that makes the same tool calls Claude would. The prompts and tools are
  ready; quality with real Claude needs trying on 10–20 real agents and tuning the system prompt.
- **Docker runner not run here** (the container registry is blocked in this workspace). Its command is
  tested; build the image and try one run on your server before relying on it. The `process` runner is
  tested and fine for your own agents, not for strangers' code.
- **One owner.** A single access token; no user accounts, teams or billing yet.
- **No connected apps yet** (Gmail, Slack, Sheets sign-in). Agents can call any API with keys stored as
  secrets; email *out* works via SMTP. Email *in* as a trigger is not built.
- **Agents can reach any website.** Outbound network isn't restricted per agent.
- **AI budget can overshoot by one call** (the limit is checked before each call).
- **MCP access** (Claude Code / Cursor building on Jinai) is left out, as agreed.

## Layout

```
backend/jinai_agents/
  api.py         HTTP API, webhooks, AI endpoint for running agents, serves the web app
  builder.py     Claude tool loop (build and repair), system prompt, tools
  repair.py      what happens after a bad run: repair → redeploy → redo, or pause + alert
  runner.py      sandboxed execution (process / docker), results, notifications
  scheduler.py   cron schedules
  store.py       versions, deploy, secrets (encrypted), Steps outline, health
  db.py          tables: agents, versions, runs, messages, events, usage
  sandbox/       boot.py + the jinai library mounted into every run
backend/scripts/demo.py   demo mode
web/                       Next.js app (static export)
deploy/                    runner image, systemd unit, nginx
```
