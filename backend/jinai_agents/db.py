"""Database tables (SQLAlchemy). SQLite for local use, PostgreSQL in production."""
from __future__ import annotations

import secrets as _secrets
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, create_engine,
                        event as sa_event)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from . import config


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Base(DeclarativeBase):
    pass


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ag"))
    name: Mapped[str] = mapped_column(String(200), default="New agent")
    description: Mapped[str] = mapped_column(Text, default="")
    # draft (never deployed) | live | paused | needs_attention
    status: Mapped[str] = mapped_column(String(20), default="draft")
    trigger_type: Mapped[str] = mapped_column(String(20), default="manual")  # manual | schedule | webhook
    schedule: Mapped[str] = mapped_column(String(100), default="")  # cron, e.g. "0 9 * * 1"
    timezone: Mapped[str] = mapped_column(String(60), default="UTC")
    webhook_secret: Mapped[str] = mapped_column(String(64), default=lambda: _secrets.token_urlsafe(18))
    alert_email: Mapped[str] = mapped_column(String(200), default="")
    max_steps: Mapped[int] = mapped_column(Integer, default=20)  # AI agent-loop steps per run
    max_cost_per_run: Mapped[float] = mapped_column(Float, default=0.5)  # USD of AI per run
    timeout_s: Mapped[int] = mapped_column(Integer, default=300)
    auto_repair: Mapped[bool] = mapped_column(Boolean, default=True)
    needed_secrets: Mapped[list] = mapped_column(JSON, default=list)
    secrets_enc: Mapped[dict] = mapped_column(JSON, default=dict)  # name -> encrypted value
    state: Mapped[dict] = mapped_column(JSON, default=dict)  # agent memory between runs
    draft_files: Mapped[dict] = mapped_column(JSON, default=dict)  # path -> content
    draft_dirty: Mapped[bool] = mapped_column(Boolean, default=False)  # draft differs from live
    live_version_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    builder_busy: Mapped[bool] = mapped_column(Boolean, default=False)
    builder_activity: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class Version(Base):
    __tablename__ = "versions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("v"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    files: Mapped[dict] = mapped_column(JSON)
    note: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[str] = mapped_column(String(20), default="user")  # user | builder | repair
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("run"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    version_number: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = draft test
    kind: Mapped[str] = mapped_column(String(20))  # schedule | manual | webhook | test
    # queued | running | success | failed | suspicious
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict | list | str | None] = mapped_column(JSON, nullable=True)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    checks: Mapped[list] = mapped_column(JSON, default=list)
    notifications: Mapped[list] = mapped_column(JSON, default=list)
    logs: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    ai_calls: Mapped[int] = mapped_column(Integer, default=0)
    ai_cost: Mapped[float] = mapped_column(Float, default=0.0)
    token: Mapped[str] = mapped_column(String(64), default=lambda: _secrets.token_urlsafe(24))
    repair_status: Mapped[str] = mapped_column(String(20), default="")  # "" | repairing | fixed | not_fixed
    parent_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)  # set when spawned by another agent
    waiting_for_human: Mapped[bool] = mapped_column(Boolean, default=False)
    human_input: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Message(Base):
    """Chat between the user and the builder, plus activity lines ("Wrote agent.py")."""
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))  # user | assistant | activity
    text: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Event(Base):
    """Things the owner should know: deployed, auto-fixed, needs you, paused."""
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # deployed | fixed | needs_you | paused | resumed
    text: Mapped[str] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class Usage(Base):
    """AI spend: builder sessions and agent runs."""
    __tablename__ = "usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # build | repair | run
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


# --------------------------------------------------------------------------- NEW: structured store
class StoreTable(Base):
    """Named table in an agent's structured store."""
    __tablename__ = "store_tables"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("st"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class StoreRow(Base):
    """A row in an agent's structured store."""
    __tablename__ = "store_rows"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("sr"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    table_name: Mapped[str] = mapped_column(String(100), index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


# --------------------------------------------------------------------------- NEW: semantic memory
class MemoryItem(Base):
    """A remembered fact for an agent (long-term semantic memory)."""
    __tablename__ = "memory_items"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("mi"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    text: Mapped[str] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    # embedding stored as JSON list of floats (small enough for SQLite)
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


# --------------------------------------------------------------------------- NEW: workspace secrets
class WorkspaceSecret(Base):
    """Shared secrets available to all agents in the workspace."""
    __tablename__ = "workspace_secrets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    encrypted_value: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


# --------------------------------------------------------------------------- NEW: human approvals
class HumanApproval(Base):
    """A pending human approval request from a paused run."""
    __tablename__ = "human_approvals"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ha"))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # pending | approved | rejected | timed_out
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    timeout_hours: Mapped[float] = mapped_column(Float, default=24.0)
    default_action: Mapped[str] = mapped_column(String(20), default="reject")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- NEW: agent-to-agent calls
class AgentCall(Base):
    """Record of one agent calling another."""
    __tablename__ = "agent_calls"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ac"))
    caller_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    callee_agent_id: Mapped[str] = mapped_column(String(40), index=True)
    caller_run_id: Mapped[str] = mapped_column(String(40), index=True)
    callee_run_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # pending | running | done | failed
    status: Mapped[str] = mapped_column(String(20), default="pending")
    wait: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


_engine = None
_Session = None


def init(url: str | None = None):
    global _engine, _Session
    url = url or config.settings.database_url
    kw = {"connect_args": {"check_same_thread": False, "timeout": 30}} if url.startswith("sqlite") else {"pool_pre_ping": True}
    _engine = create_engine(url, **kw)
    if url.startswith("sqlite"):
        @sa_event.listens_for(_engine, "connect")
        def _pragma(conn, _):
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    Base.metadata.create_all(_engine)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


@contextmanager
def session():
    if _Session is None:
        init()
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
