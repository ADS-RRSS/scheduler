"""Handing a whole queue over to Zernio in one go.

The scheduler can release videos one at a time while this app is open, or it
can compute every slot up front and leave the whole queue parked in Zernio with
its ``scheduledFor``. The second is what this module does, and it is the only
way to close the laptop and still have the queue publish.

    plan()          what each video's hour would be, without touching anything
    delegate()      commit that plan: the videos leave the local rotation
    next_handover() which one the worker uploads next
    cancel()        take one (or the whole batch) back from Zernio

The hours are the same ones the local scheduler would have used: the queue's
interval, its windows and weekdays, its daily cap and the account-wide ceiling
are all applied by the same planner (scheduler/planner.py). Delegating is
therefore a change of *who* publishes, never of *when*.

A delegated video is DELEGATED with its instant in ``scheduled_at``. Until the
bytes reach Zernio it has no ``zernio_post_id``: that is the work still to do,
and it survives a restart, so a batch interrupted half-way resumes by itself.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Account, Schedule, VideoItem, VideoStatus
from . import accounts, events, publisher, queue, scheduling
from .zernio import ZernioError

# Taking videos back from Zernio goes in rounds so a big batch does not trip its
# rate limit: this many deletes, then a pause, then the next round.
RECOVER_BATCH_SIZE = 10
RECOVER_PAUSE_SECONDS = 5.0

# A caption still being written is worth waiting for, exactly as it is when
# publishing: the same ceiling applies (Settings.caption_max_wait_minutes).
WRITING = ("pending", "generating")


@dataclass(frozen=True)
class FolderSet:
    """Several library folders (ticked, or picked by their labels) taken as one by the bulk actions."""

    folders: frozenset[str]
    # «las carpetas con «serie» y «terminada»», for the messages.
    text: str


# A library folder by name, a set of them, or ``None`` for all of them.
Scope = str | FolderSet | None


def describe(folders: list[str], labels: list[str], match: str = "all") -> str:
    """How messages name a combination of folders and labels."""
    def joined(names: list[str], word: str) -> str:
        quoted = [f"«{n}»" for n in names]
        return quoted[0] if len(quoted) == 1 else f"{', '.join(quoted[:-1])} {word} {quoted[-1]}"

    if not labels:
        return f"la carpeta «{folders[0]}»" if len(folders) == 1 else f"las carpetas {joined(folders, 'y')}"
    tagged = f"con la etiqueta {joined(labels, 'y')}" if len(labels) == 1 else (
        f"con las etiquetas {joined(labels, 'o' if match == 'any' else 'y')}")
    return f"las carpetas {joined(folders, 'y')} {tagged}" if folders else f"las carpetas {tagged}"


def in_folder(items: list[VideoItem], folder: Scope) -> list[VideoItem]:
    """The ones whose file is in this library folder (``None`` = all of them)."""
    if folder is None:
        return items
    wanted = folder.folders if isinstance(folder, FolderSet) else {folder}
    return [item for item in items
            if item.library_item is not None and item.library_item.folder in wanted]


def scope_text(folder: Scope) -> str:
    """«la carpeta «x»» or «las carpetas con la etiqueta «y»», for messages."""
    if isinstance(folder, FolderSet):
        return folder.text
    return f"la carpeta «{folder}»"


def _scope_data(folder: Scope) -> dict:
    if isinstance(folder, FolderSet):
        return {"folder": None, "folders": sorted(folder.folders)}
    return {"folder": folder}


@dataclass(frozen=True)
class PlannedPost:
    """One video and the instant Zernio would be told to publish it."""

    item: VideoItem
    when: datetime


def plan(
    session: Session,
    schedule: Schedule,
    *,
    limit: int | None = None,
    folder: Scope = None,
    now: datetime | None = None,
) -> list[PlannedPost]:
    """The hour each pending video of a queue would get. Changes nothing.

    Videos beyond what the queue's rules can place (a cap of zero, every
    weekday disabled) are simply absent from the result; the caller reports
    them as left behind rather than inventing an hour for them.

    ``folder`` plans only that library folder's videos, as if they were the
    head of the queue; the rest stay local and start after them (a queue's
    cadence counts from its last post promised to Zernio).
    """
    # «Fuera de turno» is already on its way out, outside the queue's hours.
    items = in_folder([i for i in queue.pending(session, schedule.id) if not i.extra], folder)
    if limit is not None:
        items = items[:limit]
    if not items:
        return []
    slots = scheduling.preview(
        session, schedule, count=len(items), now=now,
        # Their own current slots must not count against the plan being made
        # for them, or each video would push the next one a day forward.
        exclude_video_ids={item.id for item in items},
    )
    return [PlannedPost(item, when) for item, when in zip(items, slots)]


def spacing_minutes(planned: list[PlannedPost]) -> list[int]:
    """Gap in minutes between consecutive posts of a plan."""
    return [
        round((b.when - a.when).total_seconds() / 60)
        for a, b in zip(planned, planned[1:])
    ]


def delegate(
    session: Session,
    schedule: Schedule,
    *,
    limit: int | None = None,
    folder: Scope = None,
    now: datetime | None = None,
) -> dict:
    """Commit the plan: every video in it becomes Zernio's to publish.

    The videos leave the local rotation immediately (DELEGATED is not a pending
    status), so the scheduler will not also publish them. Their bytes have not
    left yet: the worker uploads them one by one, and until it does each one
    can still be taken back with :func:`cancel`.
    """
    planned = plan(session, schedule, limit=limit, folder=folder, now=now)
    stamp = datetime.now(timezone.utc)
    for entry in planned:
        item = entry.item
        item.status = VideoStatus.DELEGATED
        item.scheduled_at = entry.when
        item.delegated_at = stamp
        item.force_publish = False
        item.attempts = 0
        item.last_error = None
        item.zernio_post_id = None
        # Upload as soon as the worker gets to it.
        item.next_status_check_at = None
    session.flush()

    left = len(queue.pending(session, schedule.id))
    # Whatever could not be placed stays local, so the queue keeps a real slot.
    scheduling.plan_next_slot(session, schedule)

    if planned:
        first, last = planned[0].when, planned[-1].when
        events.record(
            session, "queue_delegated",
            f"{len(planned)} vídeo(s) de «{schedule.name}»"
            + (f" ({scope_text(folder)})" if folder is not None else "")
            + " entregados a Zernio: del "
            f"{scheduling.local_label(first, schedule.timezone)} al "
            f"{scheduling.local_label(last, schedule.timezone)}.",
            level="success", account_id=schedule.account_id,
            data={"count": len(planned), "first": first.isoformat(),
                  "last": last.isoformat(), "left_behind": left, **_scope_data(folder)},
        )
    return {
        "delegated": len(planned),
        "left_behind": left,
        "first": planned[0].when if planned else None,
        "last": planned[-1].when if planned else None,
        "items": [entry.item.id for entry in planned],
    }


def delegate_at(item: VideoItem, when: datetime) -> None:
    """Hand one video to Zernio for an instant the user chose, «fuera de turno».

    Like :func:`delegate`, but the hour is not the queue's: it is on top of
    its rhythm, so it fills none of its budget and moves none of its hours
    (``extra``). The worker uploads it on its next tick with ``scheduledFor``.
    """
    item.status = VideoStatus.DELEGATED
    item.scheduled_at = when.astimezone(timezone.utc)
    item.delegated_at = datetime.now(timezone.utc)
    item.extra = True
    item.priority = 0
    item.force_publish = False
    item.attempts = 0
    item.last_error = None
    item.zernio_post_id = None
    item.next_status_check_at = None


# --------------------------------------------------------------------------- #
# Handover
# --------------------------------------------------------------------------- #

def waiting_for_text(item: VideoItem, settings: Settings) -> bool:
    """Is this video still worth holding back for its automatic text?"""
    if item.caption_status not in WRITING:
        return False
    if not settings.caption_ai_enabled:
        return False
    since = item.caption_requested_at or item.created_at
    return datetime.now(timezone.utc) - since < timedelta(
        minutes=settings.caption_max_wait_minutes
    )


def next_handover(
    session: Session, account: Account, settings: Settings, now: datetime | None = None
) -> VideoItem | None:
    """The next delegated video whose bytes should go up, if any.

    Earliest hour first, so a batch reaches Zernio in the order it will be
    published and the first videos are safe even if the upload is interrupted.
    A video waiting for its text or backing off from an error is skipped, not
    blocking: the rest of the batch keeps moving.
    """
    now = now or datetime.now(timezone.utc)
    for item in queue.awaiting_handover(session, account.id):
        if item.next_status_check_at and item.next_status_check_at > now:
            continue
        if waiting_for_text(item, settings):
            continue
        if item.caption_status in WRITING:
            # Out of patience: it goes up with the text it has, same as a
            # normal publication would.
            item.caption_status = "failed"
            item.caption_error = (
                f"el texto no estuvo listo en {settings.caption_max_wait_minutes} min; "
                "se entrega a Zernio con el texto que tenía")
            events.record(
                session, "caption_timeout",
                f"«{item.original_name}» se entrega sin esperar más al texto automático.",
                level="warn", account_id=account.id, video_id=item.id,
            )
        return item
    return None


def pending_count(session: Session, account_id: int) -> int:
    """Videos of this account still waiting to be uploaded to Zernio."""
    return len(queue.awaiting_handover(session, account_id))


# --------------------------------------------------------------------------- #
# Taking it back
# --------------------------------------------------------------------------- #

async def cancel(
    session: Session,
    account: Account,
    item: VideoItem,
    settings: Settings,
    *,
    back_to_queue: bool = True,
) -> None:
    """Undo one delegation.

    Deletes the post in Zernio when it already got there, so it cannot publish
    behind our back, and puts the video back in the local queue. Raises
    :class:`ZernioError` if Zernio refuses: a video that may still be parked
    there must not be silently re-queued, or it would publish twice.

    Returns False, with the video marked PUBLISHED, when Zernio had already
    published it: nothing is left to take back.
    """
    if item.zernio_post_id:
        try:
            await accounts.client(account, settings).delete_post(item.zernio_post_id)
        except ZernioError as exc:
            if exc.code != "zernio_already_published":
                raise
            await publisher.settle_published(session, account, item, settings)
            session.flush()
            return False
    item.zernio_post_id = None
    item.delegated_at = None
    item.next_status_check_at = None
    item.attempts = 0
    if back_to_queue:
        item.status = VideoStatus.QUEUED
        item.scheduled_at = None
        item.last_error = None
        # A video programmed for its own hour comes back as an ordinary one:
        # «fuera de turno» with no hour would publish on the next tick.
        item.extra = False
    session.flush()
    return True


async def cancel_batch(
    session: Session,
    account: Account,
    settings: Settings,
    *,
    schedule: Schedule | None = None,
    folder: Scope = None,
    video_ids: set[int] | None = None,
) -> dict:
    """Bring a whole delegation back to the local queue, or part of it.

    ``folder`` takes back only that library folder's videos, ``video_ids``
    only those (a selection in the queue view); both together, the videos
    that are in both. Works in rounds of ``RECOVER_BATCH_SIZE`` with a pause of
    ``RECOVER_PAUSE_SECONDS`` between them, so Zernio's rate limit is not hit; a
    video it still rate-limits is retried once after the wait it asks for. Stops
    at the first failure and reports it: the videos it did recover are already
    back, and pressing again resumes with the rest.

    Each video is committed as soon as Zernio lets it go. One transaction for
    the whole batch would hold SQLite's write lock through every request and
    every pause — minutes for a big batch — so the worker, the captioner and
    the lease heartbeat would hit «database is locked»; the worker could even
    upload a video this batch had already taken back, and a crash half-way
    would forget recoveries Zernio had already done.

    A video Zernio has already published (its hour came before the poll saw
    it) is marked published and the batch goes on with the next one.
    """
    recovered, published, problem = 0, 0, ""
    items = in_folder(queue.delegated(session, account.id,
                                      schedule.id if schedule is not None else None), folder)
    if video_ids is not None:
        items = [item for item in items if item.id in video_ids]
    for index, item in enumerate(items):
        if index and index % RECOVER_BATCH_SIZE == 0:
            await asyncio.sleep(RECOVER_PAUSE_SECONDS)
        try:
            try:
                back = await cancel(session, account, item, settings)
            except ZernioError as exc:
                if exc.code != "zernio_rate_limit":
                    raise
                await asyncio.sleep(max(exc.retry_after or 0, RECOVER_PAUSE_SECONDS))
                back = await cancel(session, account, item, settings)
        except ZernioError as exc:
            problem = exc.message or exc.code
            break
        session.commit()
        if back:
            recovered += 1
        else:
            published += 1
    if published:
        # A publication moves every queue of the account (the shared spacing).
        scheduling.replan_account(session, account)
    elif recovered:
        for target in ([schedule] if schedule is not None else account.schedules):
            scheduling.plan_next_slot(session, target)
    if recovered or published:
        events.record(
            session, "delegation_cancelled",
            f"{recovered} vídeo(s) recuperados de Zernio"
            + (f" para la cola «{schedule.name}»" if schedule is not None else "")
            + (f" ({scope_text(folder)})" if folder is not None else "")
            + ": vuelven a la cola local."
            + (f" {published} ya estaban publicados y quedan como publicados."
               if published else ""),
            level="warn", account_id=account.id,
        )
    return {"recovered": recovered, "published": published, "problem": problem,
            "left": pending_count(session, account.id)}


async def withdraw(
    account: Account, items: list[VideoItem], settings: Settings
) -> dict:
    """Best effort before dropping delegated videos for good.

    Deletes each post in Zernio so it cannot publish once its row is gone; one
    already deleted there (by hand, in Zernio's own queue) counts as done. It
    never raises: the caller has decided to drop the rows anyway, so whatever
    Zernio refuses is reported instead. Stops at the first refusal, as
    :func:`cancel_batch` does, rather than waiting out a dead connection once
    per video.
    """
    cancelled, left, problem = 0, 0, ""
    uploaded = [item for item in items if item.zernio_post_id]
    for index, item in enumerate(uploaded):
        try:
            await accounts.client(account, settings).delete_post(item.zernio_post_id)
        except ZernioError as exc:
            if exc.code == "zernio_already_published":
                continue  # already out: nothing left to stop
            problem = exc.message or exc.code
            left = len(uploaded) - index
            break
        cancelled += 1
    return {"cancelled": cancelled, "left": left, "problem": problem}
