"""GitHub integration: OAuth, one repo per agent, push on deploy, pull on push webhook."""
from __future__ import annotations

import base64
import json
import logging
import re
import threading
import urllib.parse
import urllib.request

from sqlalchemy import select

from . import config
from .db import Agent, WorkspaceGitHub, session
from .store import _fernet

log = logging.getLogger("jinai.github")

_GITHUB_API = "https://api.github.com"
_OAUTH_AUTHORIZE = "https://github.com/login/oauth/authorize"
_OAUTH_TOKEN = "https://github.com/login/oauth/access_token"


# --------------------------------------------------------------------------- token store

def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


def get_token() -> str | None:
    """Return the decrypted workspace GitHub token, or None."""
    with session() as s:
        row = s.get(WorkspaceGitHub, 1)
        if row is None:
            return None
        try:
            return _decrypt(row.encrypted_token)
        except Exception:
            return None


def set_token(token: str, login: str = "") -> None:
    """Save or replace the workspace GitHub token."""
    from .db import WorkspaceGitHub, now
    enc = _encrypt(token)
    with session() as s:
        row = s.get(WorkspaceGitHub, 1)
        if row is None:
            s.add(WorkspaceGitHub(id=1, encrypted_token=enc, github_login=login))
        else:
            row.encrypted_token = enc
            row.github_login = login


def get_github_status() -> dict:
    """Return { connected: bool, login: str }"""
    with session() as s:
        row = s.get(WorkspaceGitHub, 1)
        if row is None:
            return {"connected": False, "login": ""}
        return {"connected": True, "login": row.github_login}


def disconnect() -> None:
    with session() as s:
        row = s.get(WorkspaceGitHub, 1)
        if row:
            s.delete(row)


# --------------------------------------------------------------------------- OAuth flow

def oauth_url(state: str) -> str:
    st = config.settings
    if not st.github_client_id:
        raise ValueError("GITHUB_CLIENT_ID not set")
    params = urllib.parse.urlencode({
        "client_id": st.github_client_id,
        "scope": "repo",
        "state": state,
        "redirect_uri": f"{st.public_url}/api/github/callback",
    })
    return f"{_OAUTH_AUTHORIZE}?{params}"


def exchange_code(code: str) -> str:
    """Exchange OAuth code for access token. Returns the token."""
    st = config.settings
    data = urllib.parse.urlencode({
        "client_id": st.github_client_id,
        "client_secret": st.github_client_secret,
        "code": code,
    }).encode()
    req = urllib.request.Request(_OAUTH_TOKEN, data=data,
                                 headers={"Accept": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.loads(r.read())
    if "access_token" not in body:
        raise ValueError(f"GitHub OAuth error: {body.get('error_description', body)}")
    return body["access_token"]


# --------------------------------------------------------------------------- GitHub API helpers

def _gh(method: str, path: str, token: str, body: dict | None = None) -> dict | list | None:
    url = path if path.startswith("http") else f"{_GITHUB_API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "jinai-agents/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read()
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"GitHub API {method} {path} → {e.code}: {body_text}") from e


def _get_login(token: str) -> str:
    return _gh("GET", "/user", token)["login"]  # type: ignore


# --------------------------------------------------------------------------- repo management

def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return slug[:100] or "agent"


def _ensure_repo(token: str, login: str, slug: str) -> str:
    """Create the repo if it doesn't exist. Returns 'owner/repo'."""
    full = f"{login}/{slug}"
    try:
        _gh("GET", f"/repos/{full}", token)
        return full  # already exists
    except RuntimeError as e:
        if "404" not in str(e):
            raise
    # Create it
    _gh("POST", "/user/repos", token, {
        "name": slug,
        "description": "jinai agent",
        "private": True,
        "auto_init": False,
    })
    return full


def _get_file_sha(token: str, repo: str, path: str) -> str | None:
    """Get the blob SHA of a file (needed to update it)."""
    try:
        r = _gh("GET", f"/repos/{repo}/contents/{path}", token)
        return r["sha"]  # type: ignore
    except RuntimeError as e:
        if "404" in str(e):
            return None
        raise


def push_files(token: str, repo: str, files: dict, message: str) -> None:
    """Push multiple files to the repo using the Git Trees API (single commit)."""
    # 1. Get current HEAD
    try:
        ref = _gh("GET", f"/repos/{repo}/git/ref/heads/main", token)
        base_sha = ref["object"]["sha"]  # type: ignore
        base_tree = _gh("GET", f"/repos/{repo}/git/commits/{base_sha}", token)["tree"]["sha"]  # type: ignore
    except RuntimeError as e:
        if "409" in str(e) or "404" in str(e):
            # Empty repo — create initial commit via contents API instead
            _push_files_initial(token, repo, files, message)
            return
        raise

    # 2. Create blobs for each file
    tree_items = []
    for path, content in files.items():
        blob = _gh("POST", f"/repos/{repo}/git/blobs", token, {
            "content": base64.b64encode(content.encode()).decode(),
            "encoding": "base64",
        })
        tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})  # type: ignore

    # 3. Create tree
    new_tree = _gh("POST", f"/repos/{repo}/git/trees", token, {
        "base_tree": base_tree,
        "tree": tree_items,
    })

    # 4. Create commit
    commit = _gh("POST", f"/repos/{repo}/git/commits", token, {
        "message": message,
        "tree": new_tree["sha"],  # type: ignore
        "parents": [base_sha],
    })

    # 5. Update HEAD
    _gh("PATCH", f"/repos/{repo}/git/refs/heads/main", token, {
        "sha": commit["sha"],  # type: ignore
    })


def _push_files_initial(token: str, repo: str, files: dict, message: str) -> None:
    """Push to an empty repo by creating files one at a time."""
    for path, content in files.items():
        sha = _get_file_sha(token, repo, path)
        body: dict = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
        }
        if sha:
            body["sha"] = sha
        _gh("PUT", f"/repos/{repo}/contents/{path}", token, body)


def get_files(token: str, repo: str) -> dict:
    """Fetch all files from the repo's main branch. Returns {path: content}."""
    try:
        tree = _gh("GET", f"/repos/{repo}/git/trees/main?recursive=1", token)
    except RuntimeError as e:
        if "404" in str(e) or "409" in str(e):
            return {}
        raise
    files = {}
    for item in tree.get("tree", []):  # type: ignore
        if item["type"] != "blob":
            continue
        path = item["path"]
        if not path.endswith((".py", ".md", ".txt", ".json", ".yaml", ".yml", ".csv")):
            continue
        blob = _gh("GET", item["url"], token)
        content = base64.b64decode(blob["content"].replace("\n", "")).decode("utf-8", "replace")  # type: ignore
        files[path] = content
    return files


def setup_webhook(token: str, repo: str, secret: str) -> None:
    """Create a push webhook on the repo (idempotent — skips if already exists)."""
    st = config.settings
    url = f"{st.public_url}/api/github/webhook"
    existing = _gh("GET", f"/repos/{repo}/hooks", token)
    for hook in (existing or []):  # type: ignore
        if hook.get("config", {}).get("url") == url:
            return  # already installed
    _gh("POST", f"/repos/{repo}/hooks", token, {
        "name": "web",
        "active": True,
        "events": ["push"],
        "config": {"url": url, "content_type": "json", "secret": secret},
    })


# --------------------------------------------------------------------------- high-level sync

def sync_agent_to_github(agent_id: str, version_note: str = "") -> str | None:
    """Push the agent's live files to its GitHub repo. Returns repo URL or None."""
    token = get_token()
    if not token:
        return None
    with session() as s:
        a = s.get(Agent, agent_id)
        if a is None:
            return None
        from .store import live_version
        v = live_version(s, a)
        if v is None:
            return None
        files = v.files
        name = a.name
        existing_repo = a.github_repo
        webhook_secret = a.webhook_secret

    login = _get_login(token)
    slug = _slugify(name)
    repo = existing_repo or _ensure_repo(token, login, slug)

    message = version_note or f"Deploy v{v.number}"
    push_files(token, repo, files, message)

    # Install webhook if not already there
    try:
        setup_webhook(token, repo, webhook_secret)
    except Exception as e:
        log.warning("Could not set up webhook for %s: %s", repo, e)

    # Save repo back to agent
    if not existing_repo:
        with session() as s:
            a = s.get(Agent, agent_id)
            if a:
                a.github_repo = repo

    return f"https://github.com/{repo}"


def handle_push_webhook(payload: dict, signature: str | None) -> str:
    """Handle a GitHub push webhook. Redeploys the matching agent."""
    import hmac, hashlib

    repo_full = payload.get("repository", {}).get("full_name", "")
    if not repo_full:
        return "no repo"

    with session() as s:
        agent = s.scalars(select(Agent).where(Agent.github_repo == repo_full)).first()
        if agent is None:
            return "unknown repo"
        agent_id = agent.id
        webhook_secret = agent.webhook_secret

    # Verify signature
    if signature and webhook_secret:
        expected = "sha256=" + hmac.new(webhook_secret.encode(), json.dumps(payload).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise PermissionError("Invalid webhook signature")

    # Pull files and redeploy in background
    def _redeploy():
        try:
            token = get_token()
            if not token:
                return
            files = get_files(token, repo_full)
            if not files or "agent.py" not in files:
                return
            from . import runner
            from .store import deploy
            with session() as s:
                a = s.get(Agent, agent_id)
                if a is None:
                    return
                a.draft_files = files
                a.draft_dirty = True
            deploy(agent_id, created_by="github")
            runner.queue_run(agent_id, "manual")
        except Exception as e:
            log.error("GitHub redeploy failed for %s: %s", agent_id, e)

    threading.Thread(target=_redeploy, daemon=True).start()
    return "queued"
