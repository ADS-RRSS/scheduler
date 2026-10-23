"""Video queue: ordering and daily accounting, plus the .txt helpers.

Videos no longer enter a queue from a file: they enter the library
(services/library.py) and a queue takes them from there. A file never moves
after that, whatever happens to the queue entry, because another queue (the
same reel on YouTube) may still need it.
"""
from __future__ import annotations

import random
import unicodedata
from collections.abc import Collection
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..models import Schedule, VideoItem, VideoStatus
from . import events, reelinfo

# Statuses that occupy a slot in the daily budget. A delegated video counts
# too: Zernio is going to publish it that day whether this app is open or not.
COMMITTED_STATUSES = (
    VideoStatus.SCHEDULED,
    VideoStatus.UPLOADING,
    VideoStatus.PROCESSING,
    VideoStatus.DELEGATED,
    VideoStatus.PUBLISHED,
)
# What the local scheduler still owns. DELEGATED is deliberately absent: those
# videos left the rotation when they were handed over.
PENDING_STATUSES = (VideoStatus.QUEUED, VideoStatus.SCHEDULED)
# The order a queue releases its videos in, and so the order a delegation
# hands them to Zernio: «Ahora» first (priority 0), then the turns the last
# shuffle dealt, then arrival for whatever came in after it. The UI mirrors
# this in app.js (releaseOrder).
RELEASE_ORDER = (
    VideoItem.priority.asc(),
    VideoItem.queue_rank.asc().nulls_last(),
    VideoItem.id.asc(),
)
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm"}
# The text that travels with a video: a caption written by hand, or a reel
# sheet (see reelinfo.py) that the captioner reads as context.
SIDECAR_SUFFIXES = (".txt", ".caption.txt")
# The image that travels with a video: its cover, sent to YouTube as the custom
# thumbnail and to TikTok as the cover. Saved as ``<video>.jpg``; ContentBuilder writes ``<video>_portada.jpg``,
# which is found too.
COVER_SUFFIXES = (".jpg", ".jpeg", ".png")
COVER_TAGS = ("", "_portada")


# --------------------------------------------------------------------------- #
# File names and the .txt that travels with a video
# --------------------------------------------------------------------------- #

def safe_filename(name: str) -> str:
    """ASCII-ish, filesystem-safe name that still resembles the original."""
    stem = Path(name).stem
    suffix = Path(name).suffix.lower()
    normalised = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "-" for c in normalised).strip("-")
    return f"{cleaned or 'video'}{suffix}"


def unique_path(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    if not target.exists():
        return target
    stem, suffix = Path(filename).stem, Path(filename).suffix
    for i in range(1, 10000):
        candidate = directory / f"{stem}-{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError("no se pudo encontrar un nombre libre")


def sidecar_text(video_path: Path) -> str:
    """Whatever the `.txt` sitting next to the video says, if there is one."""
    for suffix in SIDECAR_SUFFIXES:
        sidecar = Path(video_path).with_suffix(suffix)
        if sidecar.exists():
            try:
                return sidecar.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return ""
    return ""


def sidecar_caption(video_path: Path) -> str:
    """Caption from a `.txt` file sitting next to the video, if present.

    A reel sheet (script, draft caption, questions…) is not a caption: it is
    read by the captioner as context (:func:`read_sheet`), so here it counts
    as no text at all.
    """
    text = sidecar_text(video_path)
    return "" if reelinfo.is_sheet(text) else text


def read_sheet(video_path: Path) -> reelinfo.ReelInfo | None:
    """The reel sheet next to the video, if the `.txt` there is one."""
    text = sidecar_text(video_path)
    return reelinfo.parse(text) if text else None


def write_sidecar(video_path: Path, text: str) -> Path:
    """Save a `.txt` next to a video, under the video's (final) name."""
    target = Path(video_path).with_suffix(".txt")
    target.write_text(text, encoding="utf-8")
    return target


def remove_sidecars(video_path: Path) -> None:
    for suffix in SIDECAR_SUFFIXES:
        Path(video_path).with_suffix(suffix).unlink(missing_ok=True)
    remove_covers(video_path)


def cover_key(name: str) -> str:
    """The video an image is the cover of: its name, sans folders, extension and «_portada»."""
    stem = Path(Path(name).name).stem.lower()
    return stem[: -len("_portada")] if stem.endswith("_portada") else stem


def _cover_candidates(video_path: Path) -> list[Path]:
    video_path = Path(video_path)
    return [video_path.with_name(f"{video_path.stem}{tag}{suffix}")
            for tag in COVER_TAGS for suffix in COVER_SUFFIXES]


def cover_path(video_path: Path) -> Path | None:
    """The cover image next to the video, if there is one."""
    return next((c for c in _cover_candidates(video_path) if c.is_file()), None)


def write_cover(video_path: Path, data: bytes, suffix: str) -> Path:
    """Save the cover next to a video, under the video's (final) name, replacing any other."""
    remove_covers(video_path)
    suffix = ".png" if suffix.lower() == ".png" else ".jpg"
    target = Path(video_path).with_suffix(suffix)
    target.write_bytes(data)
    return target


def remove_covers(video_path: Path) -> None:
    for cover in _cover_candidates(video_path):
        cover.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Reading the queue
# --------------------------------------------------------------------------- #

# Only videos that will publish get a text; history rows are left alone.
TEXT_ELIGIBLE = PENDING_STATUSES


def writable():
    """Videos whose text can still change what goes out.

    A delegated video counts until its bytes reach Zernio: the handover waits
    for a text being written (delegation.waiting_for_text), so leaving it out
    here made it wait for nothing and go up with no text at all.
    """
    return or_(
        VideoItem.status.in_(TEXT_ELIGIBLE),
        and_(VideoItem.status == VideoStatus.DELEGATED, VideoItem.zernio_post_id.is_(None)),
    )


def pending(session: Session, schedule_id: int) -> list[VideoItem]:
    """Videos still waiting in one queue, in the order they will go out."""
    return list(
        session.scalars(
            select(VideoItem)
            .where(
                VideoItem.schedule_id == schedule_id,
                VideoItem.status.in_(PENDING_STATUSES),
            )
            .order_by(*RELEASE_ORDER)
        )
    )


def next_up(session: Session, schedule_id: int) -> VideoItem | None:
    return next(iter(pending(session, schedule_id)), None)


def next_in_turn(session: Session, schedule_id: int) -> VideoItem | None:
    """The head the queue's own rhythm serves: the first waiting video that is
    not going out «fuera de turno» (those never take a slot)."""
    return next((item for item in pending(session, schedule_id) if not item.extra), None)


def extra_due(session: Session, schedule_id: int, now: datetime) -> VideoItem | None:
    """A «fuera de turno» video of this queue that may go out now: its first
    attempt, or a retry whose wait is over."""
    return next((
        item for item in pending(session, schedule_id)
        if item.extra and (item.scheduled_at is None or item.scheduled_at <= now)
    ), None)


def send_now(item: VideoItem, *, extra: bool = False) -> None:
    """Make the worker publish this video at its next tick, whatever the slot.

    ``extra`` publishes it «fuera de turno»: on top of the queue's rhythm, so
    the queue's next video keeps its hour and nothing else moves. Without it
    («Ahora») the video takes its turn early and the cadence restarts from it.
    """
    item.status = VideoStatus.SCHEDULED
    item.scheduled_at = datetime.now(timezone.utc)
    item.priority = 0
    item.force_publish = True
    item.extra = extra
    item.attempts = 0
    item.last_error = None


def on_its_way(item: VideoItem) -> bool:
    """Its turn has already come: «Ahora» was pressed, or it is between retries.

    Reordering the queue leaves these at the front. A retry also holds the
    SCHEDULED embargo of its next attempt, which only means something on the
    head of the queue.
    """
    return bool(item.force_publish or item.extra or item.attempts)


def shuffle(
    session: Session, schedule_id: int, rng: random.Random | None = None
) -> tuple[list[VideoItem], list[VideoItem]]:
    """Deal the waiting videos of one queue a new random order.

    Returns ``(held, mixed)``: the videos already on their way, which keep the
    front in the order they had, and the rest in their new order. The new
    order always differs from the previous one — a shuffle that happened to
    change nothing would look broken — so ``mixed`` is empty, and nothing
    changes, when fewer than two videos are free to move.

    The caller replans the queue: its head has probably changed.
    """
    waiting = pending(session, schedule_id)
    held = [item for item in waiting if on_its_way(item)]
    free = [item for item in waiting if not on_its_way(item)]
    if len(free) < 2:
        return held, []
    rng = rng or random.SystemRandom()
    mixed = list(free)
    while mixed == free:
        rng.shuffle(mixed)
    for rank, item in enumerate(held + mixed, start=1):
        item.queue_rank = rank
    session.flush()
    return held, mixed


def unshuffle(session: Session, schedule_id: int) -> int:
    """Put a shuffled queue back in the order its videos arrived in.

    Only what is waiting moves; the videos on their way keep the front, as
    they do in a shuffle. Returns how many videos lost the turn a shuffle had
    given them. The caller replans the queue.
    """
    waiting = pending(session, schedule_id)
    held = [item for item in waiting if on_its_way(item)]
    for rank, item in enumerate(held, start=1):
        item.queue_rank = rank
    reset = 0
    for item in waiting:
        if not on_its_way(item) and item.queue_rank is not None:
            item.queue_rank = None
            reset += 1
    session.flush()
    return reset


# What a video moved by hand is left with: every waiting video in one tier, so
# the turns decide alone (``priority`` sorts before ``queue_rank``).
DEFAULT_PRIORITY = 100


def reorder(
    session: Session, schedule_id: int, ids: list[int], *, position: int | None = None,
    before: int | None = None, after: int | None = None,
) -> tuple[list[VideoItem], list[VideoItem]]:
    """Move some waiting videos of a queue by hand, as one block.

    The block keeps the order it had and lands at turn ``position`` (1 = the
    first turn; past the end = last), or right before / after the video
    ``before`` / ``after``, the way a drop in the list says it. The videos on
    their way keep the front, as in a shuffle, so nothing goes ahead of them.
    Everything waiting gets its turn written down, so whatever arrives later
    still joins the end.

    Returns ``(order, moved)``: the whole new order and the videos that moved.
    Raises ``ValueError`` when none of ``ids`` can move or the anchor is one of
    them or not waiting here. The caller replans the queue.
    """
    if sum(x is not None for x in (position, before, after)) != 1:
        raise ValueError("Di a qué turno van, o delante o detrás de qué vídeo.")
    waiting = pending(session, schedule_id)
    held = [item for item in waiting if on_its_way(item)]
    free = [item for item in waiting if not on_its_way(item)]
    wanted = set(ids)
    moving = [item for item in free if item.id in wanted]
    if not moving:
        raise ValueError("Ninguno de esos vídeos está esperando su turno en esta cola "
                         "(los que ya están saliendo no se mueven).")
    rest = [item for item in free if item.id not in wanted]
    if position is not None:
        index = position - 1 - len(held)
    else:
        anchor = before if before is not None else after
        if anchor in wanted:
            raise ValueError("No se puede poner un vídeo delante o detrás de sí mismo.")
        if any(item.id == anchor for item in held):
            index = 0
        else:
            index = next((i for i, item in enumerate(rest) if item.id == anchor), None)
            if index is None:
                raise ValueError("Ese vídeo ya no está esperando en esta cola.")
            if after is not None:
                index += 1
    index = max(0, min(index, len(rest)))
    order = held + rest[:index] + moving + rest[index:]
    for rank, item in enumerate(order, start=1):
        item.queue_rank = rank
        if not on_its_way(item):
            item.priority = DEFAULT_PRIORITY
    session.flush()
    return order, moving


def delegated(session: Session, account_id: int | None = None,
              schedule_id: int | None = None) -> list[VideoItem]:
    """Videos handed over to Zernio, in the order they will be published."""
    stmt = select(VideoItem).where(VideoItem.status == VideoStatus.DELEGATED)
    if account_id is not None:
        stmt = stmt.where(VideoItem.account_id == account_id)
    if schedule_id is not None:
        stmt = stmt.where(VideoItem.schedule_id == schedule_id)
    return list(session.scalars(stmt.order_by(VideoItem.scheduled_at, VideoItem.id)))


def awaiting_handover(session: Session, account_id: int) -> list[VideoItem]:
    """Delegated videos whose bytes have not reached Zernio yet.

    A delegation marks the whole batch at once and the worker uploads them one
    by one, so this is the work still to do — and what the UI counts to show
    "12 de 30 enviados".
    """
    return [v for v in delegated(session, account_id) if not v.zernio_post_id]


def in_flight(session: Session, account_id: int | None = None) -> list[VideoItem]:
    """Items that must finish before another one starts."""
    stmt = select(VideoItem).where(
        VideoItem.status.in_((VideoStatus.UPLOADING, VideoStatus.PROCESSING))
    )
    if account_id is not None:
        stmt = stmt.where(VideoItem.account_id == account_id)
    return list(session.scalars(stmt.order_by(VideoItem.id)))


def adopt_orphans(session: Session, schedule: Schedule) -> int:
    """Move any queue-less video of the account into ``schedule``.

    A video only ends up without a queue by deleting a ``schedules`` row by hand
    (the database browser does allow it): the foreign key nulls the column
    instead of taking the videos with it. Left alone they would be invisible in
    the UI and never publish.
    """
    lost = list(
        session.scalars(
            select(VideoItem).where(
                VideoItem.account_id == schedule.account_id, VideoItem.schedule_id.is_(None)
            )
        )
    )
    for item in lost:
        item.schedule = schedule
        # A turn dealt by another queue's shuffle means nothing here: it goes
        # by arrival, behind whatever this queue's own shuffle placed.
        item.queue_rank = None
        if item.status == VideoStatus.SCHEDULED:
            item.status = VideoStatus.QUEUED
            item.scheduled_at = None
    if lost:
        session.flush()
        events.record(
            session, "videos_adopted",
            f"{len(lost)} vídeo(s) se habían quedado sin cola y pasan a «{schedule.name}».",
            level="warn", account_id=schedule.account_id,
        )
    return len(lost)


# Everything the queue view acts on: never cut, whatever the account's size.
LIVE_STATUSES = (
    VideoStatus.QUEUED, VideoStatus.SCHEDULED, VideoStatus.UPLOADING,
    VideoStatus.PROCESSING, VideoStatus.DELEGATED,
)
HISTORY_STATUSES = (VideoStatus.PUBLISHED, VideoStatus.FAILED)


def for_state(session: Session, account_id: int, history_limit: int) -> list[VideoItem]:
    """The videos /api/state lists for one account, newest first.

    Every video still on its way (waiting, uploading, in Zernio), because the
    queue view counts, shuffles and hands over exactly those; of the history
    (published, failed), only the latest ``history_limit``, because it only
    grows. The folder and the numbers of each video come in two queries for
    all of them, not two per video on every poll.

    Everything used to be capped at the latest 500: past that, the oldest
    videos — the next ones to go out — vanished from the queue, and the
    counters were computed on what was left.
    """
    options = (selectinload(VideoItem.library_item), selectinload(VideoItem.metric))
    live = session.scalars(
        select(VideoItem)
        .where(VideoItem.account_id == account_id, VideoItem.status.in_(LIVE_STATUSES))
        .options(*options)
    ).all()
    history = session.scalars(
        select(VideoItem)
        .where(VideoItem.account_id == account_id, VideoItem.status.in_(HISTORY_STATUSES))
        .options(*options)
        .order_by(VideoItem.id.desc())
        .limit(max(0, history_limit))
    ).all()
    return sorted([*live, *history], key=lambda v: v.id, reverse=True)


def status_counts(session: Session, account_id: int) -> dict[int | None, dict[str, int]]:
    """How many videos each queue of the account has in each status, counted in SQL.

    The key ``None`` holds the rows whose queue was deleted. ``handover`` is
    the delegated ones whose bytes have not reached Zernio yet.
    """
    out: dict[int | None, dict[str, int]] = {}
    for schedule_id, status, count in session.execute(
        select(VideoItem.schedule_id, VideoItem.status, func.count())
        .where(VideoItem.account_id == account_id)
        .group_by(VideoItem.schedule_id, VideoItem.status)
    ):
        out.setdefault(schedule_id, {})[status.value] = count
    for schedule_id, count in session.execute(
        select(VideoItem.schedule_id, func.count())
        .where(VideoItem.account_id == account_id, VideoItem.status == VideoStatus.DELEGATED,
               VideoItem.zernio_post_id.is_(None))
        .group_by(VideoItem.schedule_id)
    ):
        out.setdefault(schedule_id, {})["handover"] = count
    return out


def local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The UTC instants a local calendar day starts and ends at.

    Computed from the local midnights, so DST-shortened and DST-lengthened days
    come out as the 23- or 25-hour spans they really are.
    """
    start = datetime.combine(day, time(0, 0)).replace(tzinfo=tz).astimezone(timezone.utc)
    end = (
        datetime.combine(day + timedelta(days=1), time(0, 0))
        .replace(tzinfo=tz)
        .astimezone(timezone.utc)
    )
    return start, end


def committed_on(
    session: Session,
    day: date,
    tz: ZoneInfo,
    *,
    account_id: int | None = None,
    schedule_id: int | None = None,
    exclude_video_id: int | None = None,
    exclude_video_ids: Collection[int] | None = None,
) -> int:
    """How many posts are already published or booked for a local calendar day.

    Scoped to one queue (``schedule_id``) or to a whole account across its
    queues (``account_id``). Counted in the queue's timezone, not UTC: "6 al
    día" means six Madrid days, which is what a user expects.
    """
    start, end = local_day_bounds(day, tz)
    # Both bounds are exact UTC instants, so the whole filter goes to SQL: the
    # planner can ask about hundreds of days per calculation and must not scan
    # the full queue each time.
    stamp = func.coalesce(VideoItem.published_at, VideoItem.scheduled_at)
    conditions = [
        VideoItem.status.in_(COMMITTED_STATUSES),
        # «Fuera de turno» is on top of the budget, never part of it.
        VideoItem.extra.is_not(True),
        stamp.is_not(None),
        stamp >= start,
        stamp < end,
    ]
    if account_id is not None:
        conditions.append(VideoItem.account_id == account_id)
    if schedule_id is not None:
        conditions.append(VideoItem.schedule_id == schedule_id)
    if exclude_video_id is not None:
        conditions.append(VideoItem.id != exclude_video_id)
    if exclude_video_ids:
        conditions.append(VideoItem.id.not_in(list(exclude_video_ids)))
    return session.scalar(select(func.count()).select_from(VideoItem).where(*conditions)) or 0


def committed_instants(
    session: Session,
    account_id: int,
    *,
    since: datetime | None = None,
    exclude_video_ids: Collection[int] | None = None,
) -> list[datetime]:
    """Every instant this account has already published or booked, ascending.

    These are the obstacles the planner keeps the account-wide minimum
    separation from, so two queues of one TikTok account interleave instead of
    stacking up behind each other. ``since`` trims the history: a post further
    back than the gap can never conflict with anything still to be planned.

    The two columns are read raw and merged here rather than coalesced in SQL,
    so the values keep the UTC-aware type the model declares.
    """
    stamp = func.coalesce(VideoItem.published_at, VideoItem.scheduled_at)
    conditions = [
        VideoItem.account_id == account_id,
        VideoItem.status.in_(COMMITTED_STATUSES),
        # A «fuera de turno» post keeps nobody away: it was published on top.
        VideoItem.extra.is_not(True),
        stamp.is_not(None),
    ]
    if since is not None:
        conditions.append(stamp >= since)
    if exclude_video_ids:
        conditions.append(VideoItem.id.not_in(list(exclude_video_ids)))
    rows = session.execute(
        select(VideoItem.published_at, VideoItem.scheduled_at).where(*conditions)
    )
    return sorted(published or scheduled for published, scheduled in rows)


def day_counter(
    session: Session,
    tz: ZoneInfo,
    *,
    account_id: int | None = None,
    schedule_id: int | None = None,
    exclude_video_id: int | None = None,
    exclude_video_ids: Collection[int] | None = None,
):
    """Adapter matching the planner's ``DayCounter`` signature.

    Memoised: computing one slot can probe the same date repeatedly, and a
    preview of 12 slots probes the same handful of dates over and over.

    ``exclude_video_id`` keeps an item from counting against the very slot being
    computed for it, which would otherwise shift it a day forward each time the
    schedule is recalculated. ``exclude_video_ids`` does the same for a whole
    batch: planning a delegation replans every video in it at once, so none of
    them may count against its own new slot.
    """
    cache: dict[date, int] = {}

    def counter(day: date) -> int:
        if day not in cache:
            cache[day] = committed_on(
                session, day, tz, account_id=account_id, schedule_id=schedule_id,
                exclude_video_id=exclude_video_id, exclude_video_ids=exclude_video_ids,
            )
        return cache[day]

    return counter
