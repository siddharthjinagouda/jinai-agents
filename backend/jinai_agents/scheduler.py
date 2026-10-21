"""Runs scheduled agents on time."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select

from .db import Agent, as_utc, now, session

log = logging.getLogger("jinai.scheduler")

PRESETS = {
    "every 15 minutes": "*/15 * * * *",
    "hourly": "0 * * * *",
    "daily 09:00": "0 9 * * *",
    "weekdays 09:00": "0 9 * * 1-5",
    "mondays 09:00": "0 9 * * 1",
}


def valid_cron(expr: str) -> bool:
    try:
        croniter(expr)
        return len(expr.split()) == 5
    except (ValueError, KeyError):
        return False


def next_time(expr: str, tz: str, after: datetime | None = None) -> datetime:
    zone = ZoneInfo(tz or "UTC")
    base = (as_utc(after) or now()).astimezone(zone)
    return croniter(expr, base).get_next(datetime).astimezone(timezone.utc)


def schedule_next(agent: Agent):
    if agent.trigger_type == "schedule" and agent.schedule and agent.status == "live" and agent.live_version_id:
        agent.next_run_at = next_time(agent.schedule, agent.timezone)
    else:
        agent.next_run_at = None


def tick():
    """Start every agent whose time has come."""
    from .runner import RunnerError, queue_run
    due = []
    with session() as s:
        for a in s.scalars(select(Agent).where(Agent.status == "live", Agent.trigger_type == "schedule",
                                               Agent.next_run_at.is_not(None))):
            if as_utc(a.next_run_at) <= now():
                due.append(a.id)
                a.next_run_at = next_time(a.schedule, a.timezone)
    for agent_id in due:
        try:
            queue_run(agent_id, "schedule")
        except RunnerError as e:
            log.info("skip %s: %s", agent_id, e)
    return due


_stop = threading.Event()


def start(interval: float = 15.0):
    def loop():
        while not _stop.wait(interval):
            try:
                tick()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")
    t = threading.Thread(target=loop, name="jinai-scheduler", daemon=True)
    t.start()
    return t


def stop():
    _stop.set()
