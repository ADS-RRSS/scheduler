"""Append-only activity log surfaced in the UI."""
from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import EventLog

log = logging.getLogger("scheduler")
MAX_EVENTS = 2000


def record(
    session: Session,
    kind: str,
    message: str,
    *,
    level: str = "info",
    account_id: int | None = None,
    video_id: int | None = None,
    data: dict | None = None,
) -> EventLog:
    event = EventLog(
        kind=kind, message=message, level=level,
        account_id=account_id, video_id=video_id, data=data,
    )
    session.add(event)
    session.flush()
    getattr(log, {"error": "error", "warn": "warning"}.get(level, "info"))(
        "[%s] %s", kind, message
    )
    return event


def recent(session: Session, limit: int = 100, account_id: int | None = None) -> list[EventLog]:
    stmt = select(EventLog).order_by(EventLog.id.desc()).limit(limit)
    if account_id is not None:
        stmt = stmt.where(EventLog.account_id == account_id)
    return list(session.scalars(stmt))


def search(
    session: Session, *, level: str | None = None, account_id: int | None = None,
    kind: str | None = None, q: str | None = None, limit: int = 200,
) -> list[EventLog]:
    """Filtered view for the Actividad page."""
    stmt = select(EventLog).order_by(EventLog.id.desc()).limit(max(1, min(limit, 2000)))
    if level:
        stmt = stmt.where(EventLog.level == level)
    if account_id is not None:
        stmt = stmt.where(EventLog.account_id == account_id)
    if kind:
        stmt = stmt.where(EventLog.kind == kind)
    if q:
        stmt = stmt.where(EventLog.message.ilike(f"%{q.strip()}%"))
    return list(session.scalars(stmt))


def prune_to(session: Session, keep: int) -> int:
    """Keep only the newest ``keep`` events."""
    ids = list(session.scalars(select(EventLog.id).order_by(EventLog.id.desc()).limit(max(keep, 0))))
    if not ids and keep > 0:
        return 0
    stmt = delete(EventLog)
    if ids:
        stmt = stmt.where(EventLog.id < min(ids))
    return session.execute(stmt).rowcount or 0


def prune(session: Session) -> int:
    """Keep the log bounded so the SQLite file does not grow forever."""
    ids = list(session.scalars(select(EventLog.id).order_by(EventLog.id.desc()).limit(MAX_EVENTS)))
    if len(ids) < MAX_EVENTS:
        return 0
    cutoff = min(ids)
    return session.execute(delete(EventLog).where(EventLog.id < cutoff)).rowcount or 0
