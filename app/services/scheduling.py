"""Bridge between the stored Schedule rows ("colas") and the pure planner.

An account may own several queues, each with its own hours and cadence. They
run *at the same time*, not one after the other: a second queue starts placing
videos today, weaving between what the first one already booked. They do share
one TikTok account, so on top of each queue's own rules two account-wide limits
apply, and both are folded in here rather than in the planner:

* ``Account.max_per_day`` — the queues share one daily budget;
* ``Account.min_gap_minutes`` — no two posts of the account, whatever queue
  they come from, land closer together than this. It is a distance to the
  neighbouring posts, not a turn to wait for: a queue is never pushed behind
  the *last* thing the account booked, only away from whatever sits next to
  the hour it wants. This is the only separation setting there is; a queue has
  no gap of its own, so the two can never disagree.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Account, Schedule, VideoItem, VideoStatus
from ..scheduler.planner import (
    DayLimit,
    SchedulePolicy,
    Spacing,
    build_policy,
    compute_next_slot,
    describe_policy,
    preview_slots,
)
from . import events, library, queue


def policy_from_schedule(schedule: Schedule) -> SchedulePolicy:
    return build_policy(
        interval_min_minutes=schedule.interval_min_minutes,
        interval_max_minutes=schedule.interval_max_minutes,
        # The floor under the queue's own random draw is the account's
        # separation: one number rules both the inside of a queue and the
        # distance between queues.
        min_gap_minutes=account_gap_minutes(schedule),
        jitter_seconds=schedule.jitter_seconds,
        max_per_day=schedule.max_per_day,
        windows=schedule.windows or [],
        days_of_week=schedule.days_of_week or list(range(7)),
        timezone_name=schedule.timezone,
    )


# --------------------------------------------------------------------------- #
# Account-wide limits
# --------------------------------------------------------------------------- #

def account_gap_minutes(schedule: Schedule) -> int:
    """The account's minimum separation between posts, in minutes."""
    account = schedule.account
    return max(int(account.min_gap_minutes or 0), 0) if account is not None else 0


def account_last_post_at(session: Session, account_id: int) -> datetime | None:
    """When this TikTok account last posted, from whichever queue."""
    stamps = list(
        session.scalars(
            select(Schedule.last_post_at).where(
                Schedule.account_id == account_id, Schedule.last_post_at.is_not(None)
            )
        )
    )
    return max(stamps) if stamps else None


def last_booked_at(session: Session, account_id: int, schedule_id: int) -> datetime | None:
    """The latest hour this queue already promised to Zernio, if any.

    A delegated video is a post that *will* happen at a known instant, so the
    queue's cadence has to count from it as if it had already gone out. Without
    this, a video queued after a delegation would be given a slot in the middle
    of the batch that Zernio is holding.

    Scoped to the one queue on purpose: what the *other* queues of the account
    booked is not this queue's cadence, only an obstacle to keep away from
    (see :func:`spacing_for`).
    """
    return session.scalar(
        select(func.max(VideoItem.scheduled_at)).where(
            VideoItem.account_id == account_id,
            VideoItem.schedule_id == schedule_id,
            VideoItem.status == VideoStatus.DELEGATED,
            # One programmed for its own hour is on top of the rhythm.
            VideoItem.extra.is_not(True),
        )
    )


def cadence_anchor(session: Session, schedule: Schedule) -> datetime | None:
    """What this queue's interval is measured from: its last (or next) post."""
    stamps = [
        stamp for stamp in (
            schedule.last_post_at,
            last_booked_at(session, schedule.account_id, schedule.id),
        ) if stamp is not None
    ]
    return max(stamps) if stamps else None


def local_label(when: datetime, tz_name: str) -> str:
    """'16/09 09:02' in the queue's own timezone, for logs and messages."""
    return when.astimezone(ZoneInfo(tz_name)).strftime("%d/%m %H:%M")


def spacing_for(
    session: Session, schedule: Schedule, now: datetime, *,
    exclude_video_id: int | None = None, exclude_video_ids: set[int] | None = None,
) -> Spacing:
    """Everything the account has already booked, as obstacles for the planner.

    Every queue of the account plans against the same set, so the hours one
    queue owns are holes the others plan around — which is what lets two
    queues run at once instead of one waiting for the other to finish.

    A video being (re)planned right now must not block itself, so the caller
    passes its id: without that, the head of a queue would push its own slot
    one gap further every time the queue was recalculated.
    """
    gap = account_gap_minutes(schedule)
    if not gap:
        return Spacing()
    excluded = set(exclude_video_ids or ())
    if exclude_video_id is not None:
        excluded.add(exclude_video_id)
    taken = queue.committed_instants(
        session, schedule.account_id,
        # Anything further back than one gap is already clear of everything
        # still to be planned, which starts at ``now`` at the earliest.
        since=now - timedelta(minutes=gap),
        exclude_video_ids=excluded,
    )
    # A forced post that failed leaves no committed row behind but did spend
    # the hour, and the queue recorded it: keep the account away from it too.
    last_post = account_last_post_at(session, schedule.account_id)
    if last_post is not None:
        taken.append(last_post)
    return Spacing.of(gap, taken)


def account_limit(
    session: Session, schedule: Schedule, tz: ZoneInfo, exclude_video_id: int | None = None,
    exclude_video_ids: set[int] | None = None,
) -> tuple[DayLimit, ...]:
    """The account's shared daily budget, as an extra limit for the planner.

    Counted in the timezone of the queue being planned: with the usual single
    timezone per account that is exactly the account's calendar day.
    """
    account = schedule.account
    if account is None or account.max_per_day is None:
        return ()
    return (
        DayLimit(
            max_per_day=account.max_per_day,
            counter=queue.day_counter(
                session, tz, account_id=account.id, exclude_video_id=exclude_video_id,
                exclude_video_ids=exclude_video_ids,
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

def start_of(schedule: Schedule) -> datetime | None:
    """The instant «Empezar desde» stands for: that day at its hour (00:00 without
    one), local to the queue."""
    if schedule.start_date is None:
        return None
    local = datetime.combine(schedule.start_date, schedule.start_time or time.min,
                             tzinfo=ZoneInfo(schedule.timezone))
    return local.astimezone(timezone.utc)


def earliest(schedule: Schedule, now: datetime) -> datetime:
    """The first instant this queue may book: now, or its start day if that is later."""
    start = start_of(schedule)
    return start if start is not None and start > now else now


def plan_next_slot(
    session: Session, schedule: Schedule, now: datetime | None = None
) -> datetime | None:
    """(Re)compute this queue's next slot and pin it on the head of the queue."""
    if schedule is None:
        return None

    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(schedule.timezone)
    start = start_of(schedule)
    if start is not None and start <= now:
        # The day has come: from here on the queue simply runs.
        schedule.start_date = None
        schedule.start_time = None
    # A «fuera de turno» video goes out on its own: the slot is for the
    # video whose turn it really is.
    head = queue.next_in_turn(session, schedule.id)
    exclude = head.id if head else None

    slot = compute_next_slot(
        now=earliest(schedule, now),
        last_post_at=cadence_anchor(session, schedule),
        policy=policy_from_schedule(schedule),
        day_counter=queue.day_counter(
            session, tz, schedule_id=schedule.id, exclude_video_id=exclude
        ),
        extra_limits=account_limit(session, schedule, tz, exclude_video_id=exclude),
        spacing=spacing_for(session, schedule, now, exclude_video_id=exclude),
    )
    schedule.next_slot_at = slot

    if head is not None and slot is not None:
        # A retry carries its own embargo time and is never pulled forward.
        # Only a retry: an ordinary head keeps whatever the rules say today,
        # so a slot booked under older rules (or behind another queue) is
        # brought back as soon as an earlier hour becomes free.
        if head.attempts and head.scheduled_at and head.scheduled_at > slot:
            head.status = VideoStatus.SCHEDULED
            schedule.next_slot_at = head.scheduled_at
        else:
            head.status = VideoStatus.SCHEDULED
            head.scheduled_at = slot
    session.flush()
    return schedule.next_slot_at


def invalidate(session: Session, schedule: Schedule) -> None:
    """Drop the pinned slot after a config change so it is recomputed."""
    if schedule is None:
        return
    schedule.next_slot_at = None
    for item in queue.pending(session, schedule.id):
        if item.status == VideoStatus.SCHEDULED and item.attempts == 0 and not item.extra:
            item.status = VideoStatus.QUEUED
            item.scheduled_at = None
    session.flush()


def replan_account(session: Session, account: Account, now: datetime | None = None) -> None:
    """Recompute every queue of an account, in display order.

    Called after a post goes out, after the account limits change and after a
    queue is added or removed: what one queue books moves the others.

    The order is the tie-breaker when two queues want the same hour — each one
    plans around the slots the queues before it have just pinned — so the queue
    the user put first gets first pick of a contested hour.
    """
    now = now or datetime.now(timezone.utc)
    for schedule in account.schedules:
        plan_next_slot(session, schedule, now)


def replan_all(session: Session, now: datetime | None = None) -> int:
    """Recompute every queue of every account. Returns how many were touched.

    Run once after a migration: a slot pinned under the previous version's
    rules would otherwise stay where it was until something else happened to
    move it, and the whole point of a rule change is that it applies now.
    """
    accounts = list(session.scalars(select(Account).where(Account.is_active.is_(True))))
    for account in accounts:
        replan_account(session, account, now)
    return sum(len(account.schedules) for account in accounts)


def preview(
    session: Session, schedule: Schedule, count: int = 10, now: datetime | None = None,
    exclude_video_ids: set[int] | None = None, booked: Sequence[datetime] = (),
) -> list[datetime]:
    """The next ``count`` slots of a queue, as a simulation.

    ``exclude_video_ids`` leaves a set of videos out of the daily accounting:
    used when the slots being computed are *for* those videos (delegating the
    whole queue), so they do not crowd themselves out of their own days. The
    queue's own pending videos never block it either way — see below.

    ``booked`` are instants the account's other queues were just simulated
    into (the calendar projects every queue of an account, in list order, as
    ``replan_account`` does): they fill the account's daily budget and keep
    the separation, as if they were already committed.
    """
    if schedule is None:
        return []
    tz = ZoneInfo(schedule.timezone)
    now = now or datetime.now(timezone.utc)
    # The queue's own head already holds the slot this simulation is about to
    # recompute, so it must not stand in its own way: without this the preview
    # would show a first hour one gap later than the one really pinned.
    mine = {item.id for item in queue.pending(session, schedule.id)}
    limits = account_limit(session, schedule, tz, exclude_video_ids=exclude_video_ids)
    spacing = spacing_for(session, schedule, now,
                          exclude_video_ids=mine | set(exclude_video_ids or ()))
    if booked:
        per_day = Counter(stamp.astimezone(tz).date() for stamp in booked)
        limits = tuple(
            DayLimit(limit.max_per_day,
                     lambda day, counter=limit.counter: counter(day) + per_day[day])
            for limit in limits
        )
        if spacing.gap_minutes:
            spacing = Spacing.of(spacing.gap_minutes, [*spacing.taken, *booked])
    return preview_slots(
        now=earliest(schedule, now),
        last_post_at=cadence_anchor(session, schedule),
        policy=policy_from_schedule(schedule),
        count=count,
        day_counter=queue.day_counter(
            session, tz, schedule_id=schedule.id, exclude_video_ids=exclude_video_ids
        ),
        extra_limits=limits,
        spacing=spacing,
    )


def warnings_for(schedule: Schedule) -> list[str]:
    try:
        return describe_policy(policy_from_schedule(schedule))
    except Exception as exc:  # noqa: BLE001 - surfaced as a UI warning
        return [str(exc)]


def account_warnings(account: Account) -> list[str]:
    """Warnings about how an account's queues add up."""
    notes: list[str] = []
    enabled = [s for s in account.schedules if s.enabled]
    if account.max_per_day is not None and len(enabled) > 1:
        caps = [s.max_per_day for s in enabled]
        if all(cap is not None for cap in caps) and sum(caps) > account.max_per_day:
            notes.append(
                f"Las colas activas suman {sum(caps)} vídeos/día, por encima del máximo de la "
                f"cuenta ({account.max_per_day}/día). Mandará el máximo de la cuenta: las "
                "últimas colas del día se quedarán sin hueco."
            )
        elif any(cap is None for cap in caps):
            notes.append(
                f"Alguna cola no tiene máximo diario propio; el tope real lo pone la cuenta "
                f"({account.max_per_day}/día) y se lo reparten por orden."
            )
    return notes


def is_due(schedule: Schedule, item: VideoItem | None, now: datetime) -> bool:
    """Should this item go out right now?"""
    if item is None or schedule.next_slot_at is None:
        return False
    if now < schedule.next_slot_at:
        return False
    # A retry carries its own embargo time.
    return not (item.scheduled_at and now < item.scheduled_at)


# --------------------------------------------------------------------------- #
# Queue management (create / rename / delete)
# --------------------------------------------------------------------------- #

DEFAULT_NAME = "Cola principal"
MAX_QUEUES_PER_ACCOUNT = 12

# Everything a new queue copies from the one it is duplicated from.
_COPIED_FIELDS = (
    "interval_min_minutes", "interval_max_minutes", "jitter_seconds",
    "max_per_day", "windows", "days_of_week", "timezone",
    "disable_comment", "disable_duet", "disable_stitch", "is_aigc", "brand_content_toggle",
    "brand_organic_toggle",
)


def default_schedule(account: Account) -> Schedule | None:
    return account.schedules[0] if account.schedules else None


def next_name(account: Account, wanted: str = "", *, exclude_id: int | None = None) -> str:
    """A name that is free within the account.

    Two queues with the same name would be indistinguishable in every menu and
    every notification, so a repeat gets a "(2)" suffix.
    """
    taken = {s.name.strip().lower() for s in account.schedules if s.id != exclude_id}
    base = (wanted or "").strip() or (DEFAULT_NAME if not account.schedules
                                      else f"Cola {len(account.schedules) + 1}")
    if base.lower() not in taken:
        return base[:64]
    for i in range(2, 100):
        candidate = f"{base} ({i})"
        if candidate.lower() not in taken:
            return candidate[:64]
    return base[:64]


def next_name_for(schedule: Schedule, wanted: str) -> str:
    """Like :func:`next_name`, for a rename: the queue's own name is free."""
    return next_name(schedule.account, wanted, exclude_id=schedule.id)


def create_schedule(
    session: Session,
    account: Account,
    *,
    name: str = "",
    copy_from: Schedule | None = None,
) -> Schedule:
    """Add a queue to an account, optionally cloning another one's rules."""
    if len(account.schedules) >= MAX_QUEUES_PER_ACCOUNT:
        raise ValueError(
            f"Una cuenta no puede tener más de {MAX_QUEUES_PER_ACCOUNT} colas."
        )
    position = max((s.position for s in account.schedules), default=-1) + 1
    schedule = Schedule(
        account_id=account.id,
        name=next_name(account, name),
        position=position,
        enabled=False,
    )
    if copy_from is not None:
        for field in _COPIED_FIELDS:
            value = getattr(copy_from, field)
            setattr(schedule, field, list(value) if isinstance(value, list) else value)
    session.add(schedule)
    session.flush()
    events.record(
        session, "queue_created", f"Cola creada: «{schedule.name}»",
        level="success", account_id=account.id,
    )
    return schedule


def default_for(session: Session, account: Account) -> Schedule:
    """The account's first queue, creating one if it somehow has none."""
    existing = default_schedule(account)
    if existing is not None:
        return existing
    schedule = create_schedule(session, account, name=DEFAULT_NAME)
    session.refresh(account)
    return schedule


def blockers(session: Session, schedule: Schedule) -> list[str]:
    """Reasons this queue cannot be deleted right now."""
    problems: list[str] = []
    if len(schedule.account.schedules) <= 1:
        problems.append("Una cuenta necesita al menos una cola.")
    busy = [
        item for item in queue.in_flight(session, schedule.account_id)
        if item.schedule_id == schedule.id
    ]
    if busy:
        problems.append("Hay un vídeo subiéndose en esta cola; espera a que termine.")
    return problems


def video_count(session: Session, schedule: Schedule) -> int:
    """Everything the queue holds, waiting or already published."""
    return session.scalar(
        select(func.count()).select_from(VideoItem).where(
            VideoItem.schedule_id == schedule.id
        )
    ) or 0


def delete_schedule(
    session: Session,
    schedule: Schedule,
    move_to: Schedule | None = None,
    *,
    purge: bool = False,
    delete_files: bool = False,
    force: bool = False,
) -> dict:
    """Remove a queue, and decide what happens to the videos inside it.

    ``move_to`` hands them to another queue of the same account. ``purge``
    deletes them with the queue — the rows, and with ``delete_files`` the
    files too, but only those outside the library: a library file is shared
    with the other queues and is removed from the library page, not from
    here. A queue that still holds videos needs one of the two:
    silently dropping a year of history because someone deleted a queue would
    be the worst possible default.

    Videos delegated to Zernio are refused unless ``force``: deleting their
    rows does not cancel the posts. The caller that forces is expected to have
    tried to cancel them first (delegation.withdraw); the typical case is a
    user who already emptied Zernio's queue by hand, so the rows point at
    posts that no longer exist.

    The activity log is never touched: what the account posted stays on record
    even when the queue that posted it is gone.
    """
    problems = blockers(session, schedule)
    if problems:
        raise ValueError(" ".join(problems))
    parked = queue.delegated(session, schedule.account_id, schedule.id)
    if parked and move_to is None and not force:
        # Their rows are the only record that Zernio is going to publish them,
        # and deleting the row does not cancel the post.
        raise ValueError(
            f"«{schedule.name}» tiene {len(parked)} vídeo(s) entregados a Zernio, que se "
            "publicarán aunque los borres aquí. Recupéralos primero (Cola → Recuperar de "
            "Zernio), muévelos a otra cola o marca «Borrarla igualmente»."
        )
    if move_to is not None and move_to.account_id != schedule.account_id:
        raise ValueError("La cola de destino es de otra cuenta.")
    if move_to is None and not purge and video_count(session, schedule):
        raise ValueError(
            f"«{schedule.name}» todavía tiene vídeos: elige a qué cola moverlos "
            "o confirma que quieres borrarlos con ella."
        )

    account = schedule.account
    moved, removed, files = 0, 0, 0
    for item in session.scalars(
        select(VideoItem).where(VideoItem.schedule_id == schedule.id)
    ):
        if move_to is None:
            if delete_files and Path(item.file_path).exists() \
                    and not library.owns(item.file_path):
                Path(item.file_path).unlink(missing_ok=True)
                files += 1
            session.delete(item)
            removed += 1
            continue
        # Through the relationship, not the raw id: deleting the old queue
        # would otherwise null out the videos still listed under it.
        item.schedule = move_to
        # Turns dealt by a shuffle of the old queue would cut into the new
        # one's order: they go by arrival, behind its own shuffled videos.
        item.queue_rank = None
        if item.status == VideoStatus.SCHEDULED and item.attempts == 0 and not item.extra:
            item.status = VideoStatus.QUEUED
            item.scheduled_at = None
        moved += 1
    name = schedule.name
    session.delete(schedule)
    session.flush()
    session.refresh(account)
    events.record(
        session, "queue_deleted",
        f"Cola eliminada: «{name}»"
        + (f" · {moved} vídeo(s) a «{move_to.name}»" if moved else "")
        + (f" · {removed} vídeo(s) borrados con ella" if removed else "")
        + (f", {len(parked)} de ellos entregados a Zernio"
           if removed and parked else "")
        + (f" · {files} archivo(s) borrados del disco" if files else ""),
        level="warn", account_id=account.id,
    )
    replan_account(session, account)
    return {"moved": moved, "removed": removed, "files_deleted": files}


def reorder(session: Session, account: Account, order: list[int]) -> None:
    """Apply a new display order; ids missing from ``order`` keep the tail."""
    ranks = {schedule_id: i for i, schedule_id in enumerate(order)}
    for schedule in account.schedules:
        schedule.position = ranks.get(schedule.id, len(ranks) + schedule.position)
    session.flush()
