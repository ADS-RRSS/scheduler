"""The background loop that writes the texts.

Separate from the publishing worker on purpose: transcribing a minute of
speech takes seconds to tens of seconds of CPU, and nothing about publishing
should wait for it. This loop takes the oldest video whose text is
``pending``, marks it ``generating`` so the queue shows it, runs Whisper (and
Ollama) in a thread, and stores the result — one video at a time, because two
Whisper runs at once just make both slower.

If the user edits the text while it is being generated, the result is thrown
away: their words win.
"""
from __future__ import annotations

import asyncio
import logging
import statistics
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import or_, select, update

from ..config import Settings, get_settings
from ..db import session_scope
from ..models import LibraryItem, VideoItem
from ..services import autocaption, captions, events, hashtags, library, queue

log = logging.getLogger("scheduler")

IDLE_SECONDS = 5
# How many of the latest texts the time estimate of the queue is drawn from.
DURATION_SAMPLES = 30
# Only videos that will publish get a text; history rows are left alone.
ELIGIBLE = queue.TEXT_ELIGIBLE
# Where each text came from, as the queue and the activity log say it.
SOURCE_LABELS = {
    "whisper": "Whisper", "whisper+ollama": "Whisper + Ollama",
    "ficha": "ficha .txt", "ficha+ollama": "ficha .txt + Ollama",
    "fallback": "sin voz",
}
AUTO_SOURCES = tuple(k for k in SOURCE_LABELS if k != "fallback")


def writable():
    """Videos whose text can still change what goes out (see queue.writable)."""
    return queue.writable()


def own_text():
    """Queued videos that write their own text, not the library's.

    One waiting for its library item is "pending" too, but the library item
    is the one to write: the result reaches the video through
    library.propagate.
    """
    return or_(VideoItem.caption_source.is_(None),
               VideoItem.caption_source != library.FROM_LIBRARY)


class CaptionWorker:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.current_video_id: int | None = None
        self.current_library_id: int | None = None
        self.processed = 0
        self.last_error: str | None = None
        self.last_seconds: float | None = None
        # Seconds the latest texts took, for «~2 h 10 min» next to the queue.
        self.durations: deque[float] = deque(maxlen=DURATION_SAMPLES)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self.recover()
        self._task = asyncio.create_task(self._run(), name="caption-worker")
        log.info("caption worker started (whisper=%s, ollama=%s)",
                 self.settings.whisper_model, self.settings.ollama_url or "off")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    def seconds_per_text(self) -> float | None:
        """The median time one text takes on this machine, lately; None before the first."""
        return round(statistics.median(self.durations), 1) if self.durations else None

    def _took(self, seconds: float) -> None:
        self.processed += 1
        self.last_seconds = seconds
        if seconds is not None and seconds >= 0:
            self.durations.append(float(seconds))

    @staticmethod
    def recover() -> None:
        """A crash mid-generation must not leave a video "generating" forever."""
        with session_scope() as session:
            for model in (VideoItem, LibraryItem):
                session.execute(
                    update(model)
                    .where(model.caption_status == "generating")
                    .values(caption_status="pending")
                )

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                did_work = await self.run_once()
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                did_work = False
                self.last_error = str(exc)
                log.exception("caption worker failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=1 if did_work else IDLE_SECONDS
                )
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ #
    # One video
    # ------------------------------------------------------------------ #

    async def run_once(self) -> bool:
        """Write the texts of the oldest request. False when there is none.

        Two kinds of request share the one Whisper: a library video (texts for
        every platform, which then reach the queues following it) and a queued
        video that was asked to write its own. Oldest request first.
        """
        settings = self.settings
        if not settings.caption_ai_enabled:
            return False

        job = None
        with session_scope() as session:
            # What was asked for by hand on one video goes first; then the
            # oldest request, so a folder of a hundred is written in order.
            shelved = session.scalars(
                select(LibraryItem)
                .where(LibraryItem.caption_status == "pending")
                .order_by(LibraryItem.caption_priority.desc(),
                          LibraryItem.caption_requested_at.asc().nullsfirst(),
                          LibraryItem.id.asc())
                .limit(1)
            ).first()
            item = session.scalars(
                select(VideoItem)
                .where(VideoItem.caption_status == "pending", writable(), own_text())
                .order_by(VideoItem.caption_priority.desc(),
                          VideoItem.caption_requested_at.asc().nullsfirst(), VideoItem.id.asc())
                .limit(1)
            ).first()
            if shelved is not None and (item is None or _sooner(shelved, item)):
                job = self._library_job(session, shelved)
                if job is None:
                    return True
            elif item is None:
                return False
        if job is not None:
            # Outside the transaction: generating takes seconds to minutes.
            return await self._run_library_job(*job)

        with session_scope() as session:
            item = session.get(VideoItem, item.id)
            if item is None or item.caption_status != "pending":
                return True
            if not Path(item.file_path).exists():
                item.caption_status = "failed"
                item.caption_error = "el archivo ya no está en disco"
                return True

            item.caption_status = "generating"
            video_id, path, account_id = item.id, Path(item.file_path), item.account_id
            name = item.original_name
            has_audio = bool((item.probe or {}).get("has_audio", True))
            scores, account_avg = hashtags.learned_scores(session, account_id)
            ctx = autocaption.Context(
                settings=settings,
                fixed_hashtags=captions.stored_hashtags(session),
                bank=hashtags.load_bank(settings.hashtag_bank_file),
                scores=scores,
                account_avg_views=account_avg,
                has_audio=has_audio,
                file_name=name,
                queue_name=item.schedule.name if item.schedule is not None else "",
                recent_captions=self.recent_captions(session, account_id, video_id),
                # Read now, not when the video was queued: a .txt copied into
                # the library a moment after its video still counts.
                sheet=queue.read_sheet(path),
                duration=(item.probe or {}).get("duration_sec") or None,
            )
            if has_audio and not autocaption.model_cached(settings):
                events.record(
                    session, "whisper_download",
                    f"Descargando el modelo Whisper «{settings.whisper_model}» (sólo la "
                    "primera vez; unos cientos de MB). El texto de «" + name + "» tardará algo más.",
                    account_id=account_id, video_id=video_id,
                )

        self.current_video_id = video_id
        try:
            with library.reading(path) as current:
                result = await asyncio.to_thread(autocaption.generate, current, ctx)
        except Exception as exc:  # noqa: BLE001 - stored on the video, shown in the queue
            log.warning("caption generation failed for %s: %s", name, exc)
            self._store_failure(video_id, account_id, name, autocaption_error(exc))
            return True
        finally:
            self.current_video_id = None

        self._store_result(video_id, account_id, name, result)
        return True

    # ------------------------------------------------------------------ #
    # One library video: texts for every platform
    # ------------------------------------------------------------------ #

    def _library_job(self, session, item: LibraryItem):
        """Mark a library video "generating" and gather what the model needs."""
        settings = self.settings
        if not Path(item.file_path).exists():
            item.caption_status = "failed"
            item.caption_error = "el archivo ya no está en la biblioteca"
            library.propagate(session, item)
            return None
        item.caption_status = "generating"
        path = Path(item.file_path)
        has_audio = bool((item.probe or {}).get("has_audio", True))
        # The library does not know the account yet: it learns from every
        # account of each platform, and chooses the hashtags once per platform.
        scores = {platform: hashtags.learned_scores(session, platform=platform)
                  for platform in library.PLATFORMS}
        tiktok_scores, tiktok_avg = scores["tiktok"]
        ctx = autocaption.Context(
            settings=settings,
            fixed_hashtags=captions.stored_hashtags(session),
            bank=hashtags.load_bank(settings.hashtag_bank_file),
            scores=tiktok_scores,
            account_avg_views=tiktok_avg,
            has_audio=has_audio,
            file_name=item.original_name,
            recent_captions=self.recent_library_captions(session, item),
            sheet=queue.read_sheet(path),
            duration=(item.probe or {}).get("duration_sec") or None,
        )
        if has_audio and not autocaption.model_cached(settings):
            events.record(
                session, "whisper_download",
                f"Descargando el modelo Whisper «{settings.whisper_model}» (sólo la primera "
                f"vez; unos cientos de MB). Los textos de «{item.original_name}» tardarán algo más.",
            )
        return item.id, item.original_name, path, ctx, scores

    async def _run_library_job(self, item_id: int, name: str, path: Path,
                               ctx: autocaption.Context, scores: dict) -> bool:
        self.current_library_id = item_id
        try:
            with library.reading(path) as current:
                result = await asyncio.to_thread(autocaption.generate, current, ctx)
        except Exception as exc:  # noqa: BLE001 - stored on the item, shown in the library
            log.warning("caption generation failed for %s: %s", name, exc)
            self._store_library_failure(item_id, name, autocaption_error(exc))
            return True
        finally:
            self.current_library_id = None
        self._store_library_result(item_id, name, result, ctx, scores)
        return True

    @staticmethod
    def recent_library_captions(session, item: LibraryItem) -> tuple[str, ...]:
        """The series' latest descriptions, so its videos do not all open alike."""
        rows = session.scalars(
            select(LibraryItem.tiktok_description)
            .where(LibraryItem.folder == item.folder, LibraryItem.id != item.id,
                   LibraryItem.caption_source.in_(AUTO_SOURCES),
                   LibraryItem.tiktok_description != "")
            .order_by(LibraryItem.caption_generated_at.desc().nullslast(),
                      LibraryItem.id.desc())
            .limit(autocaption.RECENT_CAPTIONS)
        ).all()
        return tuple(r for r in rows if r)

    def _store_library_result(self, item_id: int, name: str, result: autocaption.Composition,
                              ctx: autocaption.Context, scores: dict) -> None:
        with session_scope() as session:
            item = session.get(LibraryItem, item_id)
            if item is None or item.caption_status != "generating":
                # Deleted, or the user typed their own texts meanwhile.
                return
            if result.source == "ficha" and ctx.sheet is not None and ctx.sheet.has_texts:
                # No model answered: the sheet's own texts, each platform's.
                for platform, texts in library.sheet_texts(session, ctx.sheet, name).items():
                    library.set_texts(item, platform, **texts)
            else:
                for platform in library.PLATFORMS:
                    platform_scores, platform_avg = scores.get(platform, ({}, 0.0))
                    tags = (result.hashtags if platform == "tiktok" else
                            autocaption.retag(result, ctx, platform_scores, platform_avg))
                    library.set_texts(item, platform, title=result.title,
                                      description=result.caption, tags=tags,
                                      first_comment=result.first_comment)
            item.transcript = result.transcript.text or None
            item.caption_source = result.source
            item.caption_status = "done"
            item.caption_error = result.note or None
            item.caption_generated_at = datetime.now(timezone.utc)
            item.caption_priority = 0
            followers = library.propagate(session, item)
            self._took(result.seconds)
            source = SOURCE_LABELS.get(result.source, result.source)
            first = result.caption.splitlines()[0] if result.caption else ""
            message = (f"Biblioteca · textos de «{name}» ({source}, {result.seconds:g} s): "
                       f"«{result.title or first[:90]}» " + " ".join(result.hashtags))
            if followers:
                message += f" · llegan a {followers} vídeo(s) en cola"
            if result.changes:
                message += " · Cambios: " + "; ".join(result.changes)
            if result.review:
                message += f" · ⚠ {result.review}"
            events.record(
                session, "library_caption_generated", message,
                level="warn" if result.review else "success",
                data={"source": result.source, "note": result.note,
                      "changes": list(result.changes),
                      "language": result.transcript.language,
                      "words": result.transcript.words, "library_item_id": item_id,
                      "seconds": result.seconds},
            )

    @staticmethod
    def _store_library_failure(item_id: int, name: str, message: str) -> None:
        with session_scope() as session:
            item = session.get(LibraryItem, item_id)
            if item is None or item.caption_status != "generating":
                return
            item.caption_status = "failed"
            item.caption_error = message
            item.caption_priority = 0
            library.propagate(session, item)
            events.record(
                session, "library_caption_failed",
                f"Biblioteca · no se pudieron escribir los textos de «{name}»: {message}",
                level="warn",
            )

    # ------------------------------------------------------------------ #
    # One queued video that writes its own text
    # ------------------------------------------------------------------ #

    @staticmethod
    def recent_captions(session, account_id: int, video_id: int) -> tuple[str, ...]:
        """The account's latest written descriptions, so a series does not
        open every video the same way."""
        rows = session.scalars(
            select(VideoItem.caption)
            .where(VideoItem.account_id == account_id, VideoItem.id != video_id,
                   VideoItem.caption_source.in_(AUTO_SOURCES), VideoItem.caption != "")
            .order_by(VideoItem.caption_generated_at.desc().nullslast(), VideoItem.id.desc())
            .limit(autocaption.RECENT_CAPTIONS)
        ).all()
        return tuple(r for r in rows if r)

    def _store_result(self, video_id: int, account_id: int, name: str,
                      result: autocaption.Composition) -> None:
        with session_scope() as session:
            item = session.get(VideoItem, video_id)
            if item is None or item.caption_status != "generating":
                # Deleted, or the user typed their own text meanwhile.
                return
            item.title = result.title or ""
            item.caption = result.caption
            item.first_comment = result.first_comment or ""
            item.auto_hashtags = result.hashtags
            item.transcript = result.transcript.text or None
            item.caption_source = result.source
            item.caption_status = "done"
            item.caption_error = result.note or None
            item.caption_generated_at = datetime.now(timezone.utc)
            item.caption_priority = 0
            self._took(result.seconds)
            source = SOURCE_LABELS.get(result.source, result.source)
            first = result.caption.splitlines()[0] if result.caption else ""
            message = (f"Texto generado para «{name}» ({source}, {result.seconds:g} s): "
                       f"«{first[:90]}{'…' if len(first) > 90 else ''}» "
                       + " ".join(result.hashtags))
            if result.changes:
                message += " · Cambios: " + "; ".join(result.changes)
            if result.review:
                message += f" · ⚠ {result.review}"
            events.record(
                session, "caption_generated", message,
                level="warn" if result.review else "success",
                account_id=account_id, video_id=video_id,
                data={"source": result.source, "note": result.note,
                      "changes": list(result.changes),
                      "language": result.transcript.language,
                      "words": result.transcript.words, "seconds": result.seconds},
            )

    @staticmethod
    def _store_failure(video_id: int, account_id: int, name: str, message: str) -> None:
        with session_scope() as session:
            item = session.get(VideoItem, video_id)
            if item is None or item.caption_status != "generating":
                return
            item.caption_status = "failed"
            item.caption_error = message
            item.caption_priority = 0
            events.record(
                session, "caption_failed",
                f"No se pudo generar el texto de «{name}»: {message}",
                level="warn", account_id=account_id, video_id=video_id,
            )


def _sooner(shelved: LibraryItem, item: VideoItem) -> bool:
    """Does the library video go before the queued one?

    One asked for by hand goes first; between equals, the older request.
    """
    if (shelved.caption_priority or 0) != (item.caption_priority or 0):
        return (shelved.caption_priority or 0) > (item.caption_priority or 0)
    a, b = shelved.caption_requested_at, item.caption_requested_at
    if a is None or b is None:
        return b is not None or a is None
    return a <= b


def autocaption_error(exc: BaseException) -> str:
    """A message the queue can show, with the fix when there is an obvious one."""
    text = str(exc) or exc.__class__.__name__
    if isinstance(exc, ImportError) or "faster_whisper" in text:
        return ("faster-whisper no está instalado: ejecuta "
                "./.venv/bin/python -m pip install -r requirements.txt y reinicia")
    if "download" in text.lower() or "huggingface" in text.lower() or "connection" in text.lower():
        return f"no se pudo descargar el modelo Whisper (¿sin internet?): {text[:160]}"
    return text[:300]


captioner = CaptionWorker()
