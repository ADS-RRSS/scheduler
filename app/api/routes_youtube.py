"""A YouTube channel's videos and their metadata, through Zernio.

Tunnel's YouTube optimizer reads the channel here and fixes what YouTube Studio
would otherwise need by hand: category, language, playlist and thumbnail. It
works on every video of the channel, also the ones uploaded by hand, because
Zernio's ``update-metadata`` takes a YouTube video id. Nothing here publishes,
schedules or changes a video's visibility.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..models import Account
from ..schemas import YoutubeMetadataIn
from ..services import accounts as accounts_svc
from ..services import events
from ..services.zernio import YOUTUBE_CATEGORIES, YoutubeVideo, ZernioError, human_hint
from .serializers import account_label, iso

router = APIRouter(prefix="/api/youtube", tags=["youtube"])

THUMBNAIL_SUFFIXES = {".jpg", ".jpeg", ".png"}


def _youtube_account(session: Session, account_id: int) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(404, "Cuenta no encontrada")
    if (account.platform or "tiktok") != "youtube":
        raise HTTPException(400, f"{account_label(account)} no es una cuenta de YouTube")
    return account


def _zernio_error(exc: ZernioError) -> HTTPException:
    status = 429 if exc.code == "zernio_rate_limit" else 502
    return HTTPException(status, human_hint(exc))


def video_json(video: YoutubeVideo) -> dict:
    return {
        "video_id": video.video_id,
        "title": video.title,
        "published_at": iso(video.published_at),
        "url": video.url,
        "origin": video.origin,
        "post_id": video.post_id,
        "media_url": video.media_url,
        "description": video.description,
    }


@router.get("/categories")
def categories() -> dict:
    return {"categories": [{"id": k, "name": v} for k, v in YOUTUBE_CATEGORIES.items()]}


@router.get("/{account_id}/videos")
async def list_videos(account_id: int, since: datetime | None = None,
                      session: Session = Depends(get_session)) -> dict:
    """Published videos of the channel after ``since`` (all of them without it), oldest first."""
    account = _youtube_account(session, account_id)
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    try:
        client = accounts_svc.client(account, get_settings())
        videos = await client.list_youtube_videos(account.zernio_account_id, since=since)
    except ZernioError as exc:
        raise _zernio_error(exc) from exc
    return {"account": {"id": account.id, "label": account_label(account),
                        "username": account.username},
            "videos": [video_json(v) for v in videos]}


def _fields(body: YoutubeMetadataIn) -> dict:
    fields: dict = {}
    if body.category_id:
        fields["categoryId"] = body.category_id
    if body.default_language:
        fields["defaultLanguage"] = body.default_language
    if body.default_audio_language:
        fields["defaultAudioLanguage"] = body.default_audio_language
    if body.playlist_id:
        fields["playlistId"] = body.playlist_id
    return fields


def _change_text(fields: dict, thumbnail: Path | None) -> str:
    parts = []
    if "categoryId" in fields:
        parts.append(f"categoría {YOUTUBE_CATEGORIES.get(fields['categoryId'], fields['categoryId'])}")
    if "defaultLanguage" in fields or "defaultAudioLanguage" in fields:
        parts.append(f"idioma {fields.get('defaultLanguage') or fields.get('defaultAudioLanguage')}")
    if "playlistId" in fields:
        parts.append(f"lista {fields['playlistId']}")
    if thumbnail is not None:
        parts.append(f"miniatura {thumbnail.name}")
    return ", ".join(parts)


@router.post("/{account_id}/videos/{video_id}/metadata")
async def update_metadata(account_id: int, video_id: str, body: YoutubeMetadataIn,
                          session: Session = Depends(get_session)) -> dict:
    """Change one published video. ``dry_run`` answers without sending anything."""
    account = _youtube_account(session, account_id)
    fields = _fields(body)
    thumbnail: Path | None = None
    if body.thumbnail_path:
        thumbnail = Path(body.thumbnail_path).expanduser()
        if thumbnail.suffix.lower() not in THUMBNAIL_SUFFIXES:
            raise HTTPException(400, "La miniatura tiene que ser JPEG o PNG")
        if not thumbnail.is_file():
            raise HTTPException(400, f"No encuentro la miniatura: {thumbnail}")
    if not fields and thumbnail is None:
        raise HTTPException(400, "No hay nada que cambiar")
    change = _change_text(fields, thumbnail)
    if body.dry_run:
        return {"dry_run": True, "video_id": video_id, "would_send": fields,
                "thumbnail": str(thumbnail) if thumbnail else None, "change": change}

    try:
        client = accounts_svc.client(account, get_settings())
        if thumbnail is not None:
            slot = await client.presign(thumbnail)
            await client.upload(slot["uploadUrl"], thumbnail,
                                edge_host=urlsplit(slot["publicUrl"]).hostname or "")
            fields["thumbnailUrl"] = slot["publicUrl"]
        updated = await client.update_youtube_metadata(account.zernio_account_id, video_id,
                                                       fields)
    except ZernioError as exc:
        events.record(session, "youtube_metadata_failed",
                      f"No se pudo cambiar el vídeo de YouTube {video_id} ({change}): "
                      f"{exc.message or exc.code}", level="warning", account_id=account.id)
        session.commit()
        raise _zernio_error(exc) from exc
    events.record(session, "youtube_metadata_updated",
                  f"Vídeo de YouTube {video_id} de {account_label(account)}: {change}.",
                  account_id=account.id)
    session.commit()
    return {"dry_run": False, "video_id": video_id, "updated": updated, "change": change}
