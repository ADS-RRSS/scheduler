"""Publishing a single video through Zernio, end to end.

    validate -> render caption -> presign -> stream upload
    -> create post -> poll until TikTok exposes the URL

The file is never moved or deleted along the way: it belongs to the library
(services/library.py), and the same reel may still be waiting in a queue of
another platform.

The same path serves the two ways a video can leave: :func:`publish` posts it
now, and :func:`deliver_scheduled` hands it to Zernio with a future
``scheduledFor`` so Zernio publishes it by itself (see services/delegation.py).
Only the last step differs; everything before it is identical, which is why a
delegated video is validated and captioned exactly like a published one.

Every failure is classified (services/zernio.py) so the queue retries what is
worth retrying and gives up fast on what is not.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from zoneinfo import ZoneInfo

from ..config import Settings
from ..models import Account, Schedule, VideoItem, VideoStatus
from . import accounts, captions, events, library, media, notify, queue
from . import hashtags as hashtags_svc
from .zernio import PostOptions, PostResult, ZernioClient, ZernioError, human_hint

log = logging.getLogger("scheduler")

# Retry backoff: 5 min, 10 min, 20 min ... capped.
BASE_BACKOFF_MINUTES = 5
MAX_BACKOFF_MINUTES = 60
RATE_LIMIT_BACKOFF_MINUTES = 3
# How far ahead a handover is pushed when its planned instant has already
# passed: enough for Zernio to park it instead of publishing it immediately.
HANDOVER_MIN_LEAD_MINUTES = 3


class PublishOutcome:
    """Result of one attempt, so the caller knows whether to advance the slot."""

    def __init__(self, ok: bool, message: str, retry_at: datetime | None = None) -> None:
        self.ok = ok
        self.message = message
        self.retry_at = retry_at


def _backoff(attempts: int, exc: BaseException) -> datetime:
    if isinstance(exc, ZernioError) and exc.retry_after:
        seconds = min(int(exc.retry_after) + random.randint(5, 30), 3600)
        return datetime.now(timezone.utc) + timedelta(seconds=seconds)
    if isinstance(exc, ZernioError) and exc.code == "zernio_rate_limit":
        minutes = RATE_LIMIT_BACKOFF_MINUTES * max(attempts, 1)
    else:
        minutes = min(BASE_BACKOFF_MINUTES * (2 ** max(attempts - 1, 0)), MAX_BACKOFF_MINUTES)
    jitter = random.randint(0, 60)
    return datetime.now(timezone.utc) + timedelta(minutes=minutes, seconds=jitter)


def aigc_for(item: VideoItem | None, schedule: Schedule | None) -> bool:
    """Whether this video carries TikTok's «contenido generado por IA» label.

    The decision travels on the video: every import copies its queue's default
    onto ``VideoItem.is_aigc`` and the user may flip that single video
    afterwards. Only rows imported before the label existed have NULL there,
    and those still ask their queue.
    """
    if item is not None and item.is_aigc is not None:
        return bool(item.is_aigc)
    return bool(schedule.is_aigc) if schedule is not None else False


def post_options(
    schedule: Schedule,
    title: str,
    caption: str,
    first_comment: str,
    item: VideoItem | None = None,
    tags: Sequence[str] = (),
) -> PostOptions:
    """Map the queue's publishing options onto Zernio's.

    There is nothing to negotiate any more: every video goes out directly and
    publicly, which is the only thing TikTok's Business app lane publishes. The
    AI label is the one option that belongs to the video and not to the queue.
    """
    return PostOptions(
        title=title,
        caption=caption,
        first_comment=first_comment,
        tags=tuple(tags),
        disable_comment=bool(schedule.disable_comment),
        disable_duet=bool(schedule.disable_duet),
        disable_stitch=bool(schedule.disable_stitch),
        brand_content_toggle=bool(schedule.brand_content_toggle),
        brand_organic_toggle=bool(schedule.brand_organic_toggle),
        is_aigc=aigc_for(item, schedule),
    )


def _follow(item: VideoItem, current: Path) -> None:
    """Take the new path of a file whose library folder was renamed a moment ago."""
    if str(current) != item.file_path:
        item.file_path = str(current)


def prepare(
    session: Session, item: VideoItem, schedule: Schedule, hashtags: list[str] | None = None
) -> tuple[Path, PostOptions]:
    """Everything that happens before the bytes leave: validate and write.

    Mutates ``item`` with the probe, the rendered caption and any pre-flight
    error, and raises when the file cannot go out at all.
    """
    path = Path(item.file_path)
    if not path.exists():
        raise FileNotFoundError(f"el archivo ya no está en disco: {item.file_path}")

    # Re-validate the file right before it leaves.
    check = media.preflight(path)
    item.probe = check.probe.as_dict()
    if not check.ok:
        item.preflight_errors = check.errors
        raise ZernioError("file_invalid", " ".join(check.errors))

    # Caption, frozen now for the UI and the notification. A video with
    # generated hashtags carries them in the order they were chosen (fixed,
    # topic, reach); any fixed tag added to Ajustes since is appended.
    tags = hashtags_svc.with_fixed(item.auto_hashtags, hashtags)
    title = captions.render_caption(
        item.title,
        hashtags=[],
        hashtag_count=0,
        filename=item.original_name,
        index=item.id,
        timezone_name=schedule.timezone,
        keep_order=True,
    )
    caption = captions.render_caption(
        item.caption,
        hashtags=tags,
        hashtag_count=len(tags),
        filename=item.original_name,
        index=item.id,
        timezone_name=schedule.timezone,
        keep_order=bool(item.auto_hashtags),
    )
    item.rendered_title = title
    item.rendered_caption = caption

    # Tags to send as YouTube tags (without '#', distinct, preserving order).
    clean_tags = hashtags_svc.plain_tags(tags, item.caption or "")

    return path, post_options(schedule, title, caption, item.first_comment or "", item, tags=clean_tags)


async def publish(
    session: Session,
    account: Account,
    item: VideoItem,
    settings: Settings,
    hashtags: list[str] | None = None,
) -> PublishOutcome:
    """Run one publish attempt.

    Commits once, right before the upload: SQLite only lets one writer through
    at a time, and the upload can hang or take minutes, so holding this
    transaction open across it would block every other write in the app (the
    caption worker, comment sync, ...) until it times out. Whatever the caller
    still wants to persist after this returns gets its own transaction, same
    as before.
    """
    # The options come from the queue the video is in, not from the account:
    # two queues of the same account may publish very differently.
    schedule = item.schedule or account.default_schedule
    item.attempts += 1
    item.upload_started_at = datetime.now(timezone.utc)

    try:
        client = accounts.client(account, settings)
        # Held until the bytes are out, so the library does not rename its
        # folder from under the upload.
        with library.reading(item.file_path) as current:
            _follow(item, current)
            path, options = prepare(session, item, schedule, hashtags)

            # Hand the file to Zernio.
            item.status = VideoStatus.UPLOADING
            session.commit()
            result: PostResult = await client.publish(account.zernio_account_id, path, options,
                                                      platform=account.platform,
                                                      thumbnail=queue.cover_path(path))
        if result.failed:
            raise ZernioError("zernio_rejected", result.message or "Zernio rechazó el vídeo")

        item.zernio_post_id = result.post_id
        item.post_url = result.post_url

        if result.published and result.post_url:
            _finish_published(session, account, item, settings,
                              published_at=result.published_at)
            return PublishOutcome(True, "publicado a través de Zernio")

        # Accepted; the TikTok URL (or the final status) arrives a bit later.
        item.status = VideoStatus.PROCESSING
        item.last_error = None
        item.next_status_check_at = datetime.now(timezone.utc) + timedelta(
            seconds=settings.status_poll_seconds
        )
        events.record(
            session, "upload_complete",
            f"«{item.original_name}» entregado a Zernio.",
            level="success", account_id=account.id, video_id=item.id,
            data={"zernio_post_id": result.post_id},
        )
        return PublishOutcome(True, "entregado a Zernio, esperando confirmación")

    except Exception as exc:  # noqa: BLE001 - every failure is classified below
        return _handle_failure(session, account, item, exc, settings)


async def deliver_scheduled(
    session: Session,
    account: Account,
    item: VideoItem,
    settings: Settings,
    hashtags: list[str] | None = None,
) -> PublishOutcome:
    """Hand one delegated video to Zernio with its ``scheduledFor``.

    The video is already DELEGATED and carries the instant it must go out in
    ``scheduled_at``; what is missing is the upload. Zernio holds it from here,
    so this app may be closed straight after: nothing local is needed for the
    video to be published.

    Commits once, right before the upload, for the same reason ``publish``
    does: the upload can hang or take minutes, and SQLite would otherwise
    stay locked for every other writer in the app until it times out.
    """
    schedule = item.schedule or account.default_schedule
    item.attempts += 1
    item.upload_started_at = datetime.now(timezone.utc)
    when = _handover_instant(item)

    try:
        client = accounts.client(account, settings)
        with library.reading(item.file_path) as current:
            _follow(item, current)
            path, options = prepare(session, item, schedule, hashtags)
            session.commit()
            result: PostResult = await client.publish(
                account.zernio_account_id, path, options,
                scheduled_for=when, timezone_name=schedule.timezone if schedule else "",
                platform=account.platform, thumbnail=queue.cover_path(path),
            )
        if result.failed:
            raise ZernioError("zernio_rejected", result.message or "Zernio rechazó el vídeo")
        if not _still_awaiting_upload(session, item):
            return await _drop_orphan_post(session, account, item, client, result)

        item.zernio_post_id = result.post_id
        item.post_url = result.post_url
        item.scheduled_at = when
        item.last_error = None
        item.attempts = 0
        # Nothing to ask Zernio until the video was due.
        item.next_status_check_at = when + timedelta(
            minutes=settings.zernio_url_grace_minutes
        )
        if result.published:
            # Zernio published it on the spot (a slot that had already come),
            # so it went out just now — not at the hour it was handed over
            # with, which may be hours in the past.
            _finish_published(session, account, item, settings,
                              published_at=result.published_at)
            return PublishOutcome(True, "publicado a través de Zernio")

        events.record(
            session, "delegated_handover",
            f"«{item.original_name}» queda en Zernio para el "
            f"{_local(when, schedule)}.",
            level="success", account_id=account.id, video_id=item.id,
            data={"zernio_post_id": result.post_id, "scheduled_for": when.isoformat()},
        )
        return PublishOutcome(True, "programado en Zernio")

    except Exception as exc:  # noqa: BLE001 - every failure is classified below
        return _handle_handover_failure(session, account, item, exc, settings)


def _still_awaiting_upload(session: Session, item: VideoItem) -> bool:
    """Is the row still the delegated video this upload was for?

    The commit before the upload lets the rest of the app act on the video
    while its bytes travel: "Recuperar de Zernio" can put it back in the queue,
    and deleting it (or its whole queue) removes the row. Read from the
    database, not from ``item``, which still holds what this session loaded.
    """
    row = session.execute(
        select(VideoItem.status, VideoItem.zernio_post_id).where(VideoItem.id == item.id)
    ).first()
    return (row is not None and row.status == VideoStatus.DELEGATED
            and row.zernio_post_id is None)


async def _drop_orphan_post(
    session: Session, account: Account, item: VideoItem, client: ZernioClient,
    result: PostResult,
) -> PublishOutcome:
    """Cancel the post an upload just created for a video no longer delegated.

    Left in Zernio it would publish with nothing here knowing about it, and a
    video recovered into the local queue would then be published twice.
    """
    gone = session.get(VideoItem, item.id, populate_existing=True) is None
    if gone:
        # Nothing of it must be written back: the row no longer exists.
        session.expunge(item)
    what = f"«{item.original_name}» {'se borró' if gone else 'se recuperó de Zernio'}"
    problem = ""
    if result.published:
        # A slot already in the past: Zernio posted it on the spot.
        problem = "published"
        message = f"{what} mientras se subía, pero Zernio ya lo ha publicado."
    else:
        try:
            await client.delete_post(result.post_id)
            message = (f"{what} mientras se subía: el post que se acababa de crear en "
                       "Zernio se ha cancelado.")
        except ZernioError as exc:
            problem = exc.message or exc.code
            message = (f"{what} mientras se subía y su post no se pudo cancelar "
                       f"({problem}): se publicará. Bórralo desde Zernio si no quieres "
                       "que salga.")
    events.record(
        session, "delegated_orphan", message, level="error" if problem else "warn",
        account_id=account.id, video_id=None if gone else item.id,
        data={"zernio_post_id": result.post_id},
    )
    return PublishOutcome(False, message, None)


def _handover_instant(item: VideoItem) -> datetime:
    """The ``scheduledFor`` to send, never in the past.

    The batch was planned minutes or hours ago and the uploads run one at a
    time, so the first slots can go stale while the queue is still going up.
    A stale instant would make Zernio publish the moment it lands, ignoring the
    spacing, so it is nudged just far enough into the future to stay ordered.
    """
    floor = datetime.now(timezone.utc) + timedelta(minutes=HANDOVER_MIN_LEAD_MINUTES)
    planned = item.scheduled_at
    if planned is None:
        return floor
    return max(planned.astimezone(timezone.utc), floor)


def _local(when: datetime, schedule: Schedule | None) -> str:
    tz_name = schedule.timezone if schedule is not None else "UTC"
    return when.astimezone(ZoneInfo(tz_name)).strftime("%d/%m %H:%M")


def _handle_handover_failure(
    session: Session, account: Account, item: VideoItem, exc: Exception, settings: Settings
) -> PublishOutcome:
    """What a failed handover does to the video.

    A transient failure keeps its place in the batch and tries again later; a
    broken file is dropped exactly as a failed publication would drop it; and
    anything else gives the video back to the local queue, which is the state
    the user can act on.
    """
    hint = human_hint(exc)
    item.last_error = hint if str(exc) == hint else f"{exc} · {hint}"
    if not isinstance(exc, ZernioError):
        log.error("Fallo no clasificado entregando «%s» a Zernio", item.original_name,
                  exc_info=exc)

    if _retryable(exc) and item.attempts < settings.max_publish_attempts:
        retry_at = _backoff(item.attempts, exc)
        item.next_status_check_at = retry_at
        events.record(
            session, "delegate_retry",
            f"No se pudo dejar «{item.original_name}» en Zernio (intento {item.attempts}/"
            f"{settings.max_publish_attempts}): {hint} Se reintenta a las "
            f"{retry_at.astimezone().strftime('%H:%M')}.",
            level="warn", account_id=account.id, video_id=item.id,
        )
        return PublishOutcome(False, hint, retry_at)

    if isinstance(exc, (FileNotFoundError,)) or (
        isinstance(exc, ZernioError) and exc.code == "file_invalid"
    ):
        item.status = VideoStatus.FAILED
        events.record(
            session, "publish_failed", f"«{item.original_name}» descartado: {hint}",
            level="error", account_id=account.id, video_id=item.id,
            data={"error": str(exc), "attempts": item.attempts},
        )
        return PublishOutcome(False, hint, None)

    # Back to the local queue: nothing was left in Zernio, so the scheduler can
    # publish it itself, or the user can send the batch again.
    item.status = VideoStatus.QUEUED
    item.scheduled_at = None
    item.delegated_at = None
    item.next_status_check_at = None
    item.attempts = 0
    # One programmed for its own hour waits its turn now, not the next tick.
    item.extra = False
    events.record(
        session, "delegate_failed",
        f"«{item.original_name}» vuelve a la cola: {hint}",
        level="error", account_id=account.id, video_id=item.id,
        data={"error": str(exc)},
    )
    return PublishOutcome(False, hint, None)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, ZernioError):
        return exc.retryable
    if isinstance(exc, FileNotFoundError):
        return False
    # Network / timeout / OS errors.
    return True


def _handle_failure(
    session: Session, account: Account, item: VideoItem, exc: Exception, settings: Settings
) -> PublishOutcome:
    hint = human_hint(exc)
    item.last_error = hint if str(exc) == hint else f"{exc} · {hint}"
    if not isinstance(exc, ZernioError):
        log.error("Fallo no clasificado publicando «%s» en Zernio", item.original_name,
                  exc_info=exc)
    retryable = _retryable(exc) and item.attempts < settings.max_publish_attempts

    if retryable:
        retry_at = _backoff(item.attempts, exc)
        item.status = VideoStatus.SCHEDULED
        item.scheduled_at = retry_at
        events.record(
            session, "publish_retry",
            f"«{item.original_name}» falló (intento {item.attempts}/"
            f"{settings.max_publish_attempts}): {hint} Reintento a las "
            f"{retry_at.astimezone().strftime('%H:%M')}.",
            level="warn", account_id=account.id, video_id=item.id,
            data={"error": str(exc)},
        )
        return PublishOutcome(False, hint, retry_at)

    item.status = VideoStatus.FAILED
    events.record(
        session, "publish_failed",
        f"«{item.original_name}» descartado: {hint}",
        level="error", account_id=account.id, video_id=item.id,
        data={"error": str(exc), "attempts": item.attempts},
    )
    return PublishOutcome(False, hint, None)


def _when_published(item: VideoItem, happened_now: bool) -> datetime:
    """Best estimate of the instant the video actually went out.

    Only used when Zernio did not say (its webhooks carry ``publishedAt``, the
    documented REST post does not). ``happened_now`` means the publication
    completed inside the call we are returning from, so the clock is right.

    It is not right for a delegated video discovered by a later poll: Zernio
    published it while this app may have been closed for hours, and a whole
    batch caught up in one tick would otherwise share the second the app
    happened to reopen — which is exactly the hour the analytics then credit
    the views to. Its ``scheduled_at`` is the hour Zernio was told to publish
    at, and Zernio publishes there give or take: it holds posts over the
    24 h cap and releases them later, so this can read early by minutes, never
    by the hours a catch-up stamp is out by.
    """
    now = datetime.now(timezone.utc)
    if happened_now or item.status != VideoStatus.DELEGATED or item.scheduled_at is None:
        return now
    return min(item.scheduled_at.astimezone(timezone.utc), now)


def _finish_published(session: Session, account: Account, item: VideoItem,
                      settings: Settings, *, published_at: datetime | None = None,
                      happened_now: bool = True) -> None:
    # Read before the status is overwritten: it is what says whether this app
    # watched the video go out or is finding out afterwards.
    item.published_at = published_at or _when_published(item, happened_now)
    item.status = VideoStatus.PUBLISHED
    item.last_error = None
    events.record(
        session, "published", f"«{item.original_name}» publicado.",
        level="success", account_id=account.id, video_id=item.id,
        data={"url": item.post_url, "zernio_post_id": item.zernio_post_id},
    )


async def settle_published(
    session: Session, account: Account, item: VideoItem, settings: Settings
) -> None:
    """Record a delegated video Zernio already published (it refused to cancel it).

    Asks Zernio once for its link and instant, best effort: the publication
    itself is certain, so a failed lookup only leaves those blank.
    """
    result = None
    try:
        result = await accounts.client(account, settings).fetch_status(item.zernio_post_id)
    except Exception as exc:  # noqa: BLE001 - the link is a nicety
        log.warning("no se pudo consultar «%s» en Zernio: %s", item.original_name, exc)
    if result is not None and result.post_url:
        item.post_url = result.post_url
    _finish_published(session, account, item, settings,
                      published_at=result.published_at if result is not None else None,
                      happened_now=False)
    item.next_status_check_at = None
    # The cadence restarts from it, as when the worker finds it published.
    if item.schedule is not None:
        last = item.schedule.last_post_at
        if last is None or last.astimezone(timezone.utc) < item.published_at:
            item.schedule.last_post_at = item.published_at
        item.schedule.next_slot_at = None


def _waiting_since(item: VideoItem) -> datetime:
    """The instant the wait for a final status starts.

    For a delegated video that is its scheduled time, not its upload: Zernio
    holds it in between, sometimes for days, and measuring from the upload
    would declare a perfectly healthy post lost.
    """
    if item.status == VideoStatus.DELEGATED and item.scheduled_at is not None:
        return item.scheduled_at.astimezone(timezone.utc)
    return item.upload_started_at or item.created_at


def _status_deadline(item: VideoItem, settings: Settings) -> tuple[timedelta, str]:
    """How long we wait for a final status, and how to explain giving up."""
    if item.status == VideoStatus.DELEGATED:
        hours = settings.delegated_timeout_hours
        return timedelta(hours=hours), (
            f"Zernio no confirmó la publicación {hours} h después de la hora programada. "
            "Míralo en el panel de Zernio."
        )
    minutes = settings.status_timeout_minutes
    return timedelta(minutes=minutes), f"No se confirmó el estado en {minutes} minutos."


def _poll_again_in(item: VideoItem, settings: Settings, factor: int = 1) -> datetime:
    """When to ask Zernio about this post next.

    A delegated post is asked about in minutes, not seconds: there may be
    dozens of them waiting and the free plan only allows 60 requests a minute.
    """
    if item.status == VideoStatus.DELEGATED:
        gap = timedelta(minutes=settings.delegated_poll_minutes * factor)
    else:
        gap = timedelta(seconds=settings.status_poll_seconds * factor)
    return datetime.now(timezone.utc) + gap


async def check_status(
    session: Session, account: Account, item: VideoItem, settings: Settings
) -> bool:
    """Poll one post Zernio accepted. Returns True when it reached a final state.

    Zernio is always asked before the deadline is applied: the app is often
    closed for days, and on reopening, a post that went out on time is past
    the deadline too. Only a post Zernio *still* calls pending is given up on.
    """
    if not item.zernio_post_id:
        return False

    waited = datetime.now(timezone.utc) - _waiting_since(item)

    try:
        result = await accounts.client(account, settings).fetch_status(item.zernio_post_id)
    except ZernioError as exc:
        if not exc.retryable and exc.code == "zernio_auth":
            # Cannot ever confirm with this key. The video may well be live;
            # say so instead of pretending it failed.
            item.published_at = item.published_at or _when_published(item, False)
            item.status = VideoStatus.PUBLISHED
            item.last_error = f"Estado no confirmado: {exc.message}"
            events.record(session, "published",
                          f"«{item.original_name}» entregado a Zernio; no se pudo confirmar el "
                          f"estado ({exc.message}).", level="warn",
                          account_id=account.id, video_id=item.id)
            return True
        if exc.http_status == 404 and waited > _status_deadline(item, settings)[0]:
            # Zernio no longer has the post (deleted there, or never created).
            # Asking for ever would keep the account «procesando» and block
            # every other upload behind it.
            item.status = VideoStatus.FAILED
            item.last_error = ("Zernio ya no tiene este post (404): se borró allí o no llegó a "
                               "crearse. «Reintentar» lo vuelve a subir.")
            item.next_status_check_at = None
            events.record(session, "publish_failed", f"«{item.original_name}»: {item.last_error}",
                          level="error", account_id=account.id, video_id=item.id)
            return True
        item.next_status_check_at = max(
            _poll_again_in(item, settings, factor=2),
            datetime.now(timezone.utc) + timedelta(seconds=exc.retry_after or 0),
        )
        events.record(session, "status_check_failed",
                      f"No se pudo consultar el estado de «{item.original_name}»: {exc.message}",
                      level="warn", account_id=account.id, video_id=item.id)
        return False
    except Exception as exc:  # noqa: BLE001 - transient: try again next tick
        item.next_status_check_at = _poll_again_in(item, settings, factor=2)
        events.record(session, "status_check_failed",
                      f"No se pudo consultar el estado de «{item.original_name}»: {exc}",
                      level="warn", account_id=account.id, video_id=item.id)
        return False

    if result.post_url:
        item.post_url = result.post_url

    if result.failed:
        item.status = VideoStatus.FAILED
        item.last_error = result.message
        events.record(session, "publish_failed",
                      f"«{item.original_name}» rechazado: {result.message}",
                      level="error", account_id=account.id, video_id=item.id)
        return True

    if result.published and (
        result.post_url or waited > timedelta(minutes=settings.zernio_url_grace_minutes)
    ):
        _finish_published(session, account, item, settings,
                          published_at=result.published_at, happened_now=False)
        return True

    deadline, explanation = _status_deadline(item, settings)
    if waited > deadline:
        item.status = VideoStatus.FAILED
        item.last_error = explanation
        events.record(session, "publish_timeout", f"«{item.original_name}»: {item.last_error}",
                      level="error", account_id=account.id, video_id=item.id)
        return True

    item.next_status_check_at = _poll_again_in(item, settings)
    return False


async def announce(item: VideoItem, account: Account, settings: Settings,
                   webhook_url: str | None = None) -> None:
    """Send the optional notification for a video that reached a final state."""
    url = settings.webhook_url if webhook_url is None else webhook_url
    if not url:
        return
    extra: dict[str, str] = {"cuenta": account.display_name or f"@{account.username}"}
    if item.schedule is not None and len(account.schedules) > 1:
        extra["cola"] = item.schedule.name
    if item.post_url:
        extra["enlace"] = item.post_url
    if item.status == VideoStatus.FAILED:
        title = "Vídeo fallido ✕"
        extra["motivo"] = (item.last_error or "")[:300]
    else:
        title = "Vídeo publicado ✅"
    await notify.send(url, title, item.original_name, extra)


def mime_for(path: Path) -> str:
    from .zernio import mime_for as _mime

    return _mime(path)
