"""The background loop that actually makes things happen.

One tick, in order:
  1. poll posts Zernio already accepted, including the ones it is holding for a
     future hour after a whole queue was handed over
  2. register the videos copied into the library folder by hand
  3. upload one delegated video per account, or, if there is none, publish at
     most one video, from whichever of its queues came due first
  4. recompute the next slot of every queue of that account
  5. now and then, refresh analytics and comments

Step 5 runs on its own clock (hours for analytics, half-hours for comments):
they are read-only extras and must never delay or break a publication.

One upload per account per tick, whichever kind it is, bounds how long a big
file can delay status polling for everything else and keeps two queues of the
same TikTok account from ever uploading at the same time. A delegation goes
first: its hours are already promised to Zernio.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..config import Settings, get_settings
from ..db import session_scope
from ..models import Account, Schedule, Setting, VideoItem, VideoStatus
from ..services import (
    analytics,
    backups,
    captions,
    comments,
    delegation,
    events,
    library,
    notify,
    publisher,
    queue,
    scheduling,
)
from ..services.zernio import ZernioError

log = logging.getLogger("scheduler")

PAUSE_KEY = "paused"
_EVENT_PRUNE_EVERY_TICKS = 180
# A file dropped into the library folder is left alone until it has stopped
# changing for this long: registering a half-copied 500 MB video would fail
# validation for nothing.
SCAN_MIN_AGE_SECONDS = 5


def is_paused(session) -> bool:
    row = session.get(Setting, PAUSE_KEY)
    return bool(row and (row.value or {}).get("value"))


def set_paused(session, paused: bool) -> None:
    row = session.get(Setting, PAUSE_KEY)
    if row is None:
        row = Setting(key=PAUSE_KEY, value={})
        session.add(row)
    row.value = {"value": bool(paused)}
    session.flush()


class SchedulerWorker:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.started_at: datetime | None = None
        self.last_tick_at: datetime | None = None
        self.tick_count = 0
        self.last_error: str | None = None
        self.last_analytics_at: datetime | None = None
        self.last_comments_at: datetime | None = None
        # One warning in Actividad per run of failed automatic copies, not one per tick.
        self._backup_warned = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self.started_at = datetime.now(timezone.utc)
        self._task = asyncio.create_task(self._run(), name="scheduler-worker")
        log.info("scheduler worker started (tick=%ss)", self.settings.scheduler_tick_seconds)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        log.info("scheduler worker stopped")

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                self.last_error = str(exc)
                log.exception("scheduler tick failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.scheduler_tick_seconds
                )
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ #
    # One tick
    # ------------------------------------------------------------------ #

    async def tick(self) -> None:
        self.tick_count += 1
        self.last_tick_at = datetime.now(timezone.utc)

        with session_scope() as session:
            paused = is_paused(session)
            account_ids = list(
                session.scalars(
                    select(Account.id).where(Account.is_active.is_(True)).order_by(Account.id)
                )
            )
            if self.tick_count % _EVENT_PRUNE_EVERY_TICKS == 0:
                events.prune(session)

        # The library fills up whether or not anything publishes.
        if self.settings.watch_folder_enabled:
            with session_scope() as session:
                library.scan(session, library.root(self.settings),
                             min_age_seconds=SCAN_MIN_AGE_SECONDS)

        # Status polling continues even while paused: those uploads already left.
        for account_id in account_ids:
            await self._poll_in_flight(account_id)
            await self._poll_delegated(account_id)

        # Read-only extras: they follow their own clock and never raise.
        await self._refresh_analytics()
        await self._refresh_comments(account_ids)
        await self._auto_backup()

        if paused:
            return

        for account_id in account_ids:
            # A queue handed to Zernio goes up first: those hours are already
            # promised. Only when nothing is waiting does the account publish
            # by itself, so one account never uploads twice in a tick.
            if await self._hand_over(account_id):
                continue
            await self._advance_account(account_id)

    async def _poll_in_flight(self, account_id: int) -> None:
        now = datetime.now(timezone.utc)
        due: list[int] = []
        with session_scope() as session:
            for item in queue.in_flight(session, account_id):
                if item.status == VideoStatus.PROCESSING and (
                    item.next_status_check_at is None or item.next_status_check_at <= now
                ):
                    due.append(item.id)

        for video_id in due:
            with session_scope() as session:
                account = session.get(Account, account_id)
                item = session.get(VideoItem, video_id)
                if account is None or item is None:
                    continue
                finished = await publisher.check_status(session, account, item, self.settings)
                if finished and item.status == VideoStatus.PUBLISHED:
                    # The cadence restarts from the real publication instant,
                    # forced ("Ahora") or not: two videos must never go out
                    # closer together than the interval allows. Every queue of
                    # the account moves, not just this one: they share the
                    # account-wide gap and daily budget.
                    # A «fuera de turno» post went out on top of the rhythm and
                    # restarts nothing. A delegated post of the same queue may
                    # have been found published later meanwhile: the anchor
                    # never moves back.
                    if item.schedule is not None and not item.extra:
                        last = item.schedule.last_post_at
                        if last is None or last.astimezone(timezone.utc) < item.published_at:
                            item.schedule.last_post_at = item.published_at
                        item.schedule.next_slot_at = None
                    scheduling.replan_account(session, account)
                if finished:
                    webhook_url = notify.resolve_url(session, self.settings)
                    session.commit()  # the webhook is network: not with the write lock held
                    try:
                        await publisher.announce(item, account, self.settings, webhook_url)
                    except Exception as exc:  # noqa: BLE001 - notifications are optional
                        log.warning("announce failed: %s", exc)

    async def _poll_delegated(self, account_id: int) -> None:
        """Ask Zernio about the videos it is holding, once their hour has passed.

        Nothing is asked before that: the post is simply parked. This is also
        what catches up after the app has been closed for days, which is the
        whole point of delegating.
        """
        now = datetime.now(timezone.utc)
        due: list[int] = []
        with session_scope() as session:
            for item in queue.delegated(session, account_id):
                if not item.zernio_post_id:
                    continue  # not uploaded yet: that is _hand_over's job
                if item.next_status_check_at is None or item.next_status_check_at <= now:
                    due.append(item.id)

        for video_id in due:
            with session_scope() as session:
                account = session.get(Account, account_id)
                item = session.get(VideoItem, video_id)
                if account is None or item is None:
                    continue
                finished = await publisher.check_status(session, account, item, self.settings)
                if finished and item.status == VideoStatus.PUBLISHED:
                    # The cadence restarts from the real publication instant,
                    # exactly as it does for a post this app sent itself.
                    # A «fuera de turno» post went out on top of the rhythm and
                    # restarts nothing. A catch-up may find an older post
                    # after a newer one: the anchor never moves back.
                    if item.schedule is not None and not item.extra:
                        last = item.schedule.last_post_at
                        if last is None or last.astimezone(timezone.utc) < item.published_at:
                            item.schedule.last_post_at = item.published_at
                        item.schedule.next_slot_at = None
                    scheduling.replan_account(session, account)
                if finished:
                    webhook_url = notify.resolve_url(session, self.settings)
                    session.commit()  # the webhook is network: not with the write lock held
                    try:
                        await publisher.announce(item, account, self.settings, webhook_url)
                    except Exception as exc:  # noqa: BLE001 - notifications are optional
                        log.warning("announce failed: %s", exc)

    async def _hand_over(self, account_id: int) -> bool:
        """Upload the delegated videos of this account that are due to go up.

        ``DELEGATE_BATCH_PER_TICK`` of them per tick (one by default), so the
        queue view fills up visibly and a 4 GB file never blocks the rest.
        Returns True when something was uploaded, which tells the caller this
        account has had its turn.
        """
        settings = self.settings
        done = False

        for _ in range(max(settings.delegate_batch_per_tick, 1)):
            with session_scope() as session:
                account = session.get(Account, account_id)
                if account is None or queue.in_flight(session, account_id):
                    return done
                item = delegation.next_handover(session, account, settings)
                video_id = item.id if item is not None else None

            if video_id is None:
                return done

            with session_scope() as session:
                account = session.get(Account, account_id)
                item = session.get(VideoItem, video_id)
                if account is None or item is None:
                    return done
                hashtags = captions.stored_hashtags(session)
                webhook_url = notify.resolve_url(session, settings)
                outcome = await publisher.deliver_scheduled(
                    session, account, item, settings, hashtags
                )
                done = True
                # A handover that failed for good (a broken file, or a video
                # sent back to the queue) moves everything else: the hours it
                # was going to use are free again.
                if not outcome.ok and outcome.retry_at is None:
                    scheduling.replan_account(session, account)
                    if item.status == VideoStatus.FAILED:
                        session.commit()  # the webhook is network: not with the write lock held
                        try:
                            await publisher.announce(item, account, settings, webhook_url)
                        except Exception as exc:  # noqa: BLE001 - optional
                            log.warning("announce failed: %s", exc)
        return done

    async def _advance_account(self, account_id: int) -> None:
        settings = self.settings
        video_id: int | None = None
        schedule_id: int | None = None

        with session_scope() as session:
            account = session.get(Account, account_id)
            if account is None or not account.schedules:
                return

            # A video whose queue was deleted by hand would be invisible and
            # never publish: give it back to the first queue.
            queue.adopt_orphans(session, account.schedules[0])

            # Never start a second upload while one is still in flight, in any
            # queue: one TikTok account uploads one video at a time.
            if queue.in_flight(session, account_id):
                return

            now = datetime.now(timezone.utc)
            heads = [(s, queue.next_up(session, s.id)) for s in account.schedules]

            # «Ahora» wins over the clock, whichever queue it was pressed in;
            # then a «fuera de turno» video whose retry wait is over.
            forced = next(((s, h) for s, h in heads if h is not None and h.force_publish), None)
            if forced is None:
                forced = next(((s, x) for s in account.schedules
                               if (x := queue.extra_due(session, s.id, now)) is not None), None)
            if forced is not None:
                schedule, head = forced
            else:
                # The queues run at the same time, so several of them can be
                # due in one tick while only one video may go up. The oldest
                # slot wins — not the first queue in the list, which would let
                # a busy queue keep the others waiting for ever — and the
                # display order breaks a tie.
                due = []
                # The rhythm serves the videos whose turn it is, never an extra.
                heads = [(s, queue.next_in_turn(session, s.id)) for s in account.schedules]
                for schedule, head in heads:
                    if head is None or not schedule.enabled:
                        continue
                    if schedule.next_slot_at is None:
                        scheduling.plan_next_slot(session, schedule, now)
                    if scheduling.is_due(schedule, head, now):
                        due.append((schedule, head))
                if not due:
                    return
                schedule, head = min(
                    due, key=lambda pair: (pair[0].next_slot_at, pair[0].position)
                )

            # A video whose text is still being written waits for it, for a
            # while: generating at ingest only makes sense if the text is there
            # when the slot comes. Past the limit it goes out with what it has.
            if head.caption_status in ("pending", "generating"):
                if not settings.caption_ai_enabled:
                    head.caption_status = "none"
                elif _waited(head) < timedelta(minutes=settings.caption_max_wait_minutes):
                    return
                else:
                    head.caption_status = "failed"
                    head.caption_error = (
                        f"el texto no estuvo listo en {settings.caption_max_wait_minutes} "
                        "min; se publica con el texto que tenía")
                    events.record(
                        session, "caption_timeout",
                        f"«{head.original_name}» sale sin esperar más al texto automático.",
                        level="warn", account_id=account_id, video_id=head.id,
                    )

            schedule_id, video_id = schedule.id, head.id

        if video_id is None or schedule_id is None:
            return

        with session_scope() as session:
            account = session.get(Account, account_id)
            item = session.get(VideoItem, video_id)
            schedule = session.get(Schedule, schedule_id)
            if account is None or item is None or schedule is None:
                return

            was_forced = item.force_publish
            item.force_publish = False
            hashtags = captions.stored_hashtags(session)
            webhook_url = notify.resolve_url(session, settings)

            outcome = await publisher.publish(session, account, item, settings, hashtags)

            # A scheduled slot is spent either way: a failed attempt must not
            # let the next video fire immediately. A forced "Ahora" that fails
            # does not burn the scheduled slot; one that succeeds restarts the
            # cadence when its publication is confirmed (see _poll_in_flight).
            if not was_forced and not item.extra:
                schedule.last_post_at = datetime.now(timezone.utc)
                schedule.next_slot_at = None
            # Even a forced post moves the account's other queues: they share
            # the minimum gap and the daily budget.
            scheduling.replan_account(session, account)

            if (not outcome.ok and outcome.retry_at is None) or (
                outcome.ok and item.status == VideoStatus.PUBLISHED
            ):
                if not outcome.ok:
                    events.record(
                        session, "slot_consumed",
                        f"Hueco consumido por un fallo en «{item.original_name}».",
                        level="warn", account_id=account_id, video_id=item.id,
                    )
                session.commit()  # the webhook is network: not with the write lock held
                try:
                    await publisher.announce(item, account, settings, webhook_url)
                except Exception as exc:  # noqa: BLE001 - notifications are optional
                    log.warning("announce failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Analytics and comments
    # ------------------------------------------------------------------ #

    @staticmethod
    def _due(last: datetime | None, minutes: int) -> bool:
        if minutes <= 0:
            return False
        return last is None or (
            datetime.now(timezone.utc) - last >= timedelta(minutes=minutes)
        )

    async def _refresh_analytics(self) -> None:
        """Pull the per-post numbers from Zernio every few hours."""
        if not self._due(self.last_analytics_at, self.settings.analytics_refresh_minutes):
            return
        # Stamped before the work: a failure must not turn into a retry loop.
        self.last_analytics_at = datetime.now(timezone.utc)
        try:
            with session_scope() as session:
                updated, problems = await analytics.sync_all(session, self.settings)
                if updated:
                    events.record(session, "analytics_refreshed",
                                  f"Analíticas actualizadas ({updated} vídeo(s)).")
                for problem in problems:
                    events.record(session, "analytics_failed",
                                  f"No se pudieron leer las analíticas: {problem}",
                                  level="warn")
        except Exception as exc:  # noqa: BLE001 - never break the tick
            log.warning("analytics refresh failed: %s", exc)

    async def _auto_backup(self) -> None:
        """The daily copy of the database, rotated. Never breaks a tick."""
        try:
            done = await asyncio.to_thread(backups.run_auto, self.settings)
        except Exception as exc:  # noqa: BLE001 - a failed copy must not stop publishing
            log.warning("automatic backup failed: %s", exc)
            if not self._backup_warned:
                self._backup_warned = True
                with session_scope() as session:
                    events.record(session, "backup_failed",
                                  f"No se pudo hacer la copia automática de la base de datos: {exc}",
                                  level="warn")
            return
        self._backup_warned = False
        if done is not None:
            name, size, pruned = done
            log.info("automatic backup %s (%.1f MB, %s old one(s) removed)",
                     name, size / (1024 * 1024), pruned)

    async def _refresh_comments(self, account_ids: list[int]) -> None:
        """Mirror the comments of the recent videos of every active account."""
        settings = self.settings
        if not settings.comments_enabled:
            return
        if not self._due(self.last_comments_at, settings.comments_poll_minutes):
            return
        self.last_comments_at = datetime.now(timezone.utc)
        for account_id in account_ids:
            try:
                with session_scope() as session:
                    account = session.get(Account, account_id)
                    if account is None:
                        continue
                    new = await comments.sync_account(
                        session, account, settings, limit=settings.comments_batch
                    )
                    if new:
                        events.record(
                            session, "comments_received",
                            f"{new} comentario(s) nuevo(s) en "
                            f"{account.display_name or account.username}.",
                            account_id=account_id,
                        )
            except ZernioError as exc:
                with session_scope() as session:
                    events.record(session, "comments_failed", exc.message, level="warn",
                                  account_id=account_id)
            except Exception as exc:  # noqa: BLE001 - never break the tick
                log.warning("comment sync failed for account %s: %s", account_id, exc)


def _waited(item: VideoItem) -> timedelta:
    """How long a video has been waiting for its text."""
    since = item.caption_requested_at or item.created_at
    return datetime.now(timezone.utc) - since


worker = SchedulerWorker()
