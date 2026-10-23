"""The editor's helpers: review a text, fix it, suggest hashtags, draft one with the model.

Nothing here saves anything. The editor sends what it holds, gets back what is
wrong with it (textcheck.check), a fixed copy (textcheck.autofix), hashtags
worth adding, or a fresh draft from Ollama; saving is still the PATCH of the
video or of the library item, as it always was.
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..models import LibraryItem, VideoItem
from ..scheduler.captioner import CaptionWorker
from ..schemas import TextCheckIn, TextSuggestIn
from ..services import analytics, autocaption, captions, hashtags, library, queue, textcheck

router = APIRouter(prefix="/api/text", tags=["text"])

SERIES_SUGGESTIONS = 12
TOPIC_SUGGESTIONS = 6


@dataclass
class Subject:
    """What is being edited, and what the review compares it with."""

    item: LibraryItem | None
    video: VideoItem | None
    folder: str
    file_name: str
    file_path: str
    timezone: str

    @property
    def transcript(self) -> str:
        return (self.video.transcript if self.video is not None and self.video.transcript
                else self.item.transcript if self.item is not None else "") or ""


def _subject(session: Session, library_id: int | None, video_id: int | None) -> Subject:
    settings = get_settings()
    video = None
    if video_id is not None:
        video = session.get(VideoItem, video_id)
        if video is None:
            raise HTTPException(404, "Vídeo no encontrado")
    item = None
    if library_id is not None:
        item = session.get(LibraryItem, library_id)
        if item is None:
            raise HTTPException(404, "Vídeo no encontrado en la biblioteca")
    elif video is not None:
        item = video.library_item
    tz = (video.schedule.timezone if video is not None and video.schedule is not None
          else settings.default_timezone)
    source = video if video is not None else item
    return Subject(
        item=item, video=video, folder=item.folder if item is not None else "",
        file_name=source.original_name if source is not None else "",
        file_path=source.file_path if source is not None else "", timezone=tz,
    )


def _sheet(subject: Subject):
    path = Path(subject.file_path) if subject.file_path else None
    return queue.read_sheet(path) if path is not None and path.exists() else None


def _source_text(subject: Subject) -> str:
    """What the video says: its sheet's exact script, or what Whisper heard."""
    sheet = _sheet(subject)
    return sheet.script if sheet is not None and sheet.script else subject.transcript


def _siblings(session: Session, subject: Subject, platform: str) -> tuple[str, ...]:
    """The first lines of the series' other descriptions, most recent first."""
    if subject.item is not None and subject.item.folder:
        column = getattr(LibraryItem, f"{platform}_description")
        rows = session.scalars(
            select(column)
            .where(LibraryItem.folder == subject.item.folder, LibraryItem.id != subject.item.id,
                   column != "")
            .order_by(LibraryItem.caption_generated_at.desc().nullslast(), LibraryItem.id.desc())
            .limit(autocaption.RECENT_CAPTIONS)
        ).all()
        return textcheck.sibling_openings(list(rows))
    if subject.video is not None and subject.video.schedule_id is not None:
        rows = session.scalars(
            select(VideoItem.caption)
            .where(VideoItem.schedule_id == subject.video.schedule_id,
                   VideoItem.id != subject.video.id, VideoItem.caption != "")
            .order_by(VideoItem.id.desc())
            .limit(autocaption.RECENT_CAPTIONS)
        ).all()
        return textcheck.sibling_openings(list(rows))
    return ()


def _context(session: Session, subject: Subject, platform: str) -> textcheck.Context:
    return textcheck.Context(
        fixed=tuple(captions.stored_hashtags(session)),
        source=_source_text(subject),
        siblings=_siblings(session, subject, platform),
        file_name=subject.file_name,
        timezone=subject.timezone,
    )


def _texts(body: TextCheckIn) -> textcheck.Texts:
    return textcheck.Texts.of(body.title, body.description, body.tags, body.first_comment)


# --------------------------------------------------------------------------- #
# Review and fixes
# --------------------------------------------------------------------------- #

@router.post("/check")
def check_text(body: TextCheckIn, session: Session = Depends(get_session)) -> dict:
    """What is wrong with these texts, and how the post will read."""
    subject = _subject(session, body.library_id, body.video_id)
    ctx = _context(session, subject, body.platform)
    return textcheck.check(_texts(body), body.platform, ctx).as_dict()


@router.post("/fix")
def fix_text(body: TextCheckIn, session: Session = Depends(get_session)) -> dict:
    """The same texts with every fix that needs no model applied (not saved)."""
    subject = _subject(session, body.library_id, body.video_id)
    ctx = _context(session, subject, body.platform)
    fixed, changes = textcheck.autofix(_texts(body), body.platform, ctx)
    return {"texts": fixed.as_dict(), "changes": changes,
            "report": textcheck.check(fixed, body.platform, ctx).as_dict()}


# --------------------------------------------------------------------------- #
# Hashtags worth adding
# --------------------------------------------------------------------------- #

def _series_tags(session: Session, subject: Subject, platform: str) -> list[tuple[str, int]]:
    """The tags the folder's other videos carry on this platform, most used first."""
    if subject.item is None or not subject.item.folder:
        return []
    column = getattr(LibraryItem, f"{platform}_tags")
    counts: Counter[str] = Counter()
    spelling: dict[str, str] = {}
    for tags in session.scalars(select(column).where(
            LibraryItem.folder == subject.item.folder, LibraryItem.id != subject.item.id)):
        for tag in hashtags.normalize_all(tags or []):
            key = hashtags.fold(tag)
            counts[key] += 1
            spelling.setdefault(key, tag)
    return [(spelling[key], n) for key, n in counts.most_common(SERIES_SUGGESTIONS)]


@router.get("/hashtags")
def hashtag_suggestions(
    platform: str = "tiktok", library_id: int | None = None, video_id: int | None = None,
    current: str = "", text: str = "", session: Session = Depends(get_session),
) -> dict:
    """Tags to offer in the editor: proven on the platform, used by the series, on topic.

    ``current`` (space separated) are the tags already in the editor, left out
    of the answer; ``text`` is the description being typed, which joins what
    the video says when looking for the topic.
    """
    if platform not in textcheck.PLATFORMS:
        raise HTTPException(400, "platform debe ser tiktok o youtube.")
    settings = get_settings()
    subject = _subject(session, library_id, video_id)
    fixed = captions.stored_hashtags(session)
    bank = hashtags.load_bank(settings.hashtag_bank_file)
    topic_text = " ".join(filter(None, [_source_text(subject), text]))
    return {
        "fixed": fixed,
        "max": hashtags.TIKTOK_MAX_TAGS,
        "suggestions": textcheck.suggestions(
            current=current.split(), fixed=fixed,
            series=_series_tags(session, subject, platform),
            proven=analytics.hashtag_stats(session, platform),
            topic=hashtags.category_tags(topic_text, bank, limit=TOPIC_SUGGESTIONS)
            if topic_text else [],
        ),
    }


# --------------------------------------------------------------------------- #
# A fresh draft from the model
# --------------------------------------------------------------------------- #

@router.post("/suggest")
async def suggest_text(body: TextSuggestIn, session: Session = Depends(get_session)) -> dict:
    """«✨ Proponer con IA»: Ollama writes the texts again, now, and nothing is saved.

    Unlike «Regenerar», it does not wait its turn behind the library's queue
    and does not listen to the video again: it writes from the sheet's script,
    or from the transcript Whisper already left, or — a text written by hand,
    with neither — from the texts in the editor. With Ollama busy writing the
    library, the answer waits for the text in progress (one at a time).
    """
    settings = get_settings()
    if not settings.caption_ai_enabled:
        raise HTTPException(400, "Los textos automáticos están desactivados "
                                 "(CAPTION_AI_ENABLED=false en .env).")
    subject = _subject(session, body.library_id, body.video_id)
    sheet = _sheet(subject)
    transcript_text = sheet.script if sheet is not None and sheet.script else subject.transcript
    source = "ficha" if sheet is not None else "transcripción"
    if not transcript_text:
        # A text written by hand: the model improves it, from nothing else.
        own = "\n".join(filter(None, [body.title.strip(), body.description.strip()]))
        if not own:
            raise HTTPException(409, "Este vídeo no tiene ficha .txt, ni transcripción, ni texto: "
                                     "escribe algo o pulsa «Reescribir» para que Whisper lo escuche.")
        transcript_text, source = own, "tu texto"
    status = await asyncio.to_thread(autocaption.probe_ollama, settings)
    if not status.reachable:
        raise HTTPException(409, status.error or "Ollama no responde: arráncalo "
                                                 "(ollama serve) o revisa OLLAMA_URL.")

    platform = body.platform
    if subject.video is not None:
        scores, average = hashtags.learned_scores(session, subject.video.account_id)
        recent = CaptionWorker.recent_captions(session, subject.video.account_id,
                                               subject.video.id)
        queue_name = subject.video.schedule.name if subject.video.schedule is not None else ""
    else:
        scores, average = hashtags.learned_scores(session, platform=platform)
        recent = CaptionWorker.recent_library_captions(session, subject.item) \
            if subject.item is not None else ()
        queue_name = ""
    ctx = autocaption.Context(
        settings=settings,
        fixed_hashtags=captions.stored_hashtags(session),
        bank=hashtags.load_bank(settings.hashtag_bank_file),
        scores=scores,
        account_avg_views=average,
        file_name=subject.file_name,
        queue_name=queue_name,
        recent_captions=tuple(recent),
        sheet=sheet,
    )
    transcript = autocaption.Transcript(text=transcript_text, language="es")
    # Nothing more is read from the database: let go of the read snapshot
    # before waiting for the model, which may take a minute.
    session.rollback()
    started = time.monotonic()
    draft, why = await asyncio.to_thread(
        autocaption.write_with_ollama, status, ctx, transcript=transcript, sheet=sheet,
        temperatures=(0.6,),
    )
    if draft is None:
        raise HTTPException(422, why)
    tags = hashtags.select_tags(
        fixed=ctx.fixed_hashtags, niche=list(draft.hashtags), transcript=transcript_text,
        bank=ctx.bank, scores=scores, account_avg=average,
        limit=min(settings.hashtag_max, hashtags.TIKTOK_MAX_TAGS), reach=settings.hashtag_reach,
    )
    title = draft.title or autocaption._shorten(autocaption._tidy_line(draft.hook),
                                                autocaption.TITLE_MAX_CHARS)
    return {
        "platform": platform,
        "title": title,
        "description": draft.caption,
        "tags": tags,
        "first_comment": draft.first_comment,
        "changes": list(draft.changes),
        "model": status.model,
        "source": source,
        "seconds": round(time.monotonic() - started, 1),
        "platform_label": library.PLATFORM_LABELS.get(platform, platform),
    }
