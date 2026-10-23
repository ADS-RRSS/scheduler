"""The week at a glance: what went out, what Zernio holds, what comes next.

Every mark sits at its instant in one timezone (the app's default), in the
lane of its account, so the gaps between posts — and two accounts posting at
the same minute — are visible at once:

* ``published`` — when it went out (``published_at``);
* ``delegated`` — handed to Zernio with its ``scheduled_at``; Zernio publishes
  it by itself (``handover`` while its bytes are still on their way up);
* ``scheduled`` — the head of a local queue, at the slot it is pinned to;
* ``uploading`` — leaving right now;
* ``failed`` — at the slot it failed on;
* ``projected`` — every other video waiting in an *enabled* queue, at the
  hour its queue's rules would give it today. A simulation, nothing is
  written: it is ``scheduling.preview`` for each queue, and the queues of one
  account are simulated in list order with each one dodging what the previous
  ones were just given (``booked``), which is how ``replan_account`` plans
  them for real. It moves as soon as the rules, the queues or their order do.

Nothing here writes to the database.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, selectinload

from ..models import Account, VideoItem, VideoStatus
from . import queue, scheduling

# A queue of 900 videos would take a while to simulate to the end, and a week
# never holds that many: this many per queue is plenty for a month.
PROJECTION_CAP = 500
MAX_DAYS = 31
DAY_ABBR = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")

_KIND_OF = {
    VideoStatus.DELEGATED: "delegated",
    VideoStatus.SCHEDULED: "scheduled",
    VideoStatus.UPLOADING: "uploading",
    VideoStatus.PROCESSING: "uploading",
    VideoStatus.FAILED: "failed",
    VideoStatus.PUBLISHED: "published",
}


def week_start(day: date) -> date:
    """The Monday of the week ``day`` is in."""
    return day - timedelta(days=day.weekday())


def _mark(item: VideoItem, when: datetime, kind: str, tz: ZoneInfo) -> dict:
    local = when.astimezone(tz)
    folder = item.library_item.folder if item.library_item is not None else None
    return {
        "video_id": item.id,
        "account_id": item.account_id,
        "schedule_id": item.schedule_id,
        "queue": item.schedule.name if item.schedule is not None else None,
        "name": item.original_name,
        "folder": folder,
        "kind": kind,
        "at": when.astimezone(timezone.utc).isoformat(),
        "day": local.date().isoformat(),
        "minute": local.hour * 60 + local.minute,
        "time": local.strftime("%H:%M"),
        "url": item.post_url,
    }


def _kind(item: VideoItem) -> str:
    if item.status == VideoStatus.DELEGATED and not item.zernio_post_id:
        return "handover"
    return _KIND_OF.get(item.status, "scheduled")


def week(
    session: Session,
    accounts: list[Account],
    *,
    start: date,
    days: int = 7,
    tz_name: str = "Europe/Madrid",
    project: bool = True,
    paused: bool = False,
    now: datetime | None = None,
) -> dict:
    """Every mark of ``days`` days from ``start``, for these accounts."""
    tz = ZoneInfo(tz_name)
    days = max(1, min(int(days), MAX_DAYS))
    now = now or datetime.now(timezone.utc)
    begin, _ = queue.local_day_bounds(start, tz)
    _, end = queue.local_day_bounds(start + timedelta(days=days - 1), tz)
    ids = [a.id for a in accounts]
    marks: list[dict] = []
    notes: list[str] = []

    if ids:
        rows = session.scalars(
            select(VideoItem)
            .options(selectinload(VideoItem.library_item), selectinload(VideoItem.schedule))
            .where(
                VideoItem.account_id.in_(ids),
                or_(
                    and_(VideoItem.status == VideoStatus.PUBLISHED,
                         VideoItem.published_at >= begin, VideoItem.published_at < end),
                    and_(VideoItem.status.in_(tuple(s for s in _KIND_OF
                                                    if s != VideoStatus.PUBLISHED)),
                         VideoItem.scheduled_at >= begin, VideoItem.scheduled_at < end),
                ),
            )
        ).all()
        for item in rows:
            when = item.published_at if item.status == VideoStatus.PUBLISHED else item.scheduled_at
            marks.append(_mark(item, when, _kind(item), tz))

    stopped = capped = 0
    if project and end > now:
        for account in accounts:
            if not account.is_active:
                continue
            booked: list[datetime] = []
            for schedule in account.schedules:
                # «Fuera de turno» takes no slot: it is going out right now.
                waiting = [i for i in queue.pending(session, schedule.id) if not i.extra]
                if not waiting:
                    continue
                if not schedule.enabled:
                    stopped += 1
                    continue
                batch = waiting[:PROJECTION_CAP]
                capped += len(waiting) > PROJECTION_CAP
                slots = scheduling.preview(
                    session, schedule, count=len(batch), now=now,
                    exclude_video_ids={item.id for item in batch}, booked=booked,
                )
                for item, when in zip(batch, slots):
                    if item.status == VideoStatus.SCHEDULED and item.scheduled_at:
                        # The head: its real slot is already a mark (and a row the
                        # next queue plans around), not a projection.
                        continue
                    booked.append(when)
                    if begin <= when < end:
                        marks.append(_mark(item, when, "projected", tz))

    marks.sort(key=lambda m: (m["at"], m["account_id"]))
    totals: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for m in marks:
        if m["kind"] != "failed":
            totals[m["day"]][m["account_id"]] += 1

    today = now.astimezone(tz).date()
    local_now = now.astimezone(tz)
    if paused and any(m["kind"] in ("projected", "scheduled") for m in marks):
        notes.append("La programación está pausada: lo previsto y lo programado aquí no sale "
                     "hasta que la reanudes. Lo que está en Zernio sí se publica.")
    if stopped:
        notes.append(f"{stopped} cola(s) parada(s) con vídeos esperando: no se prevé nada de "
                     "ellas hasta que las actives.")
    if capped:
        notes.append(f"Sólo se prevén los {PROJECTION_CAP} primeros vídeos de cada cola.")
    return {
        "timezone": tz_name,
        "start": start.isoformat(),
        "days": [
            {"date": (start + timedelta(days=i)).isoformat(),
             "label": f"{DAY_ABBR[(start + timedelta(days=i)).weekday()]} "
                      f"{(start + timedelta(days=i)):%d/%m}",
             "today": start + timedelta(days=i) == today}
            for i in range(days)
        ],
        "today": today.isoformat(),
        "now_minute": local_now.hour * 60 + local_now.minute,
        "accounts": [
            {"id": a.id, "platform": a.platform or "tiktok", "max_per_day": a.max_per_day,
             "is_active": a.is_active}
            for a in accounts
        ],
        "marks": marks,
        "totals": {day: {str(k): v for k, v in per.items()} for day, per in totals.items()},
        "notes": notes,
    }
