"""The shared library: upload folders, read and edit the texts, queue from here."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..models import LibraryItem, Schedule, VideoItem
from ..schemas import (EnqueueIn, FolderLabelsIn, FolderRenameIn, LabelColorIn, LibraryAiIn, LibraryBulkIn,
                       LibraryMoveIn, LibraryPatch, LibraryTagsIn)
from ..services import captions, delegation, events, hashtags, library, media, publisher, queue, scheduling
from .serializers import library_json, video_json

router = APIRouter(prefix="/api", tags=["library"])

MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024
# A caption or a reel sheet is a few KB; anything bigger is not a text.
MAX_SIDECAR_BYTES = 256 * 1024
# A cover is a few hundred KB (YouTube takes up to 2 MB; Zernio compresses bigger ones).
MAX_COVER_BYTES = 20 * 1024 * 1024
MAX_LIST = 2000


def _item_or_404(session: Session, item_id: int) -> LibraryItem:
    item = session.get(LibraryItem, item_id)
    if item is None:
        raise HTTPException(404, "Vídeo no encontrado en la biblioteca")
    return item


def _tz() -> str:
    return get_settings().default_timezone


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

@router.get("/library")
def library_overview(account_id: int | None = None,
                     session: Session = Depends(get_session)) -> dict:
    """The folders, with their counts, and the totals.

    ``account_id`` adds, per folder and per label, how many videos that account
    can still take.
    """
    settings = get_settings()
    folders = library.folders(session, account_id)
    labels: dict[str, dict] = {}
    colors = library.label_colors(session)
    for f in folders:
        for label in f["labels"]:
            row = labels.setdefault(label, {"label": label, "folders": 0, "videos": 0,
                                            "color": colors.get(label)})
            row["folders"] += 1
            row["videos"] += f["count"]
            if account_id is not None:
                row["available"] = row.get("available", 0) + f["available"]
    return {
        "root": str(library.root(settings)),
        "folders": folders,
        "total": sum(f["count"] for f in folders),
        "bytes": sum(f["bytes"] for f in folders),
        "writing": sum(f["writing"] for f in folders),
        "labels": [labels[k] for k in sorted(labels)],
        # Renamed folders (series -> folder now): where an upload's videos will land.
        "aliases": library.aliases(session),
        "ai_enabled": settings.caption_ai_enabled,
        # The switch «Reescribir los textos con IA» (off: the sheets' texts as they come).
        "ai_rewrite": library.ai_rewrites(session),
    }


@router.post("/library/ai-texts")
def library_ai_texts(body: LibraryAiIn, session: Session = Depends(get_session)) -> dict:
    """Turn the switch «Reescribir los textos con IA» on or off.

    It applies to what enters from now on — uploads, a .txt that arrives
    later, files copied into media/library — never to the texts already
    written: for those there are «Reescribir textos» and «Textos de la ficha».
    """
    enabled = library.set_ai_rewrites(session, body.enabled)
    events.record(session, "library_ai_texts",
                  "Biblioteca · " + ("Ollama reescribirá los textos de las fichas .txt que entren"
                                     if enabled else
                                     "los vídeos con ficha .txt saldrán con los textos de la ficha, tal cual"))
    session.commit()
    return {"ai_rewrite": enabled}


@router.get("/library/items")
def library_items(
    folder: str | None = None,
    q: str | None = None,
    account_id: int | None = None,
    queue: str | None = None,
    accounts: list[int] | None = Query(None),
    text: str | None = None,
    label: str | None = None,
    folders: list[str] | None = Query(None),
    labels: list[str] | None = Query(None),
    match: str = Query("all", pattern="^(all|any)$"),
    exclude: list[str] | None = Query(None),
    unlabeled: str = Query("", pattern="^(include|exclude)?$"),
    issues: bool = False,
    limit: int = 500,
    session: Session = Depends(get_session),
) -> dict:
    """The videos of one folder (or matching ``q``), with where each is queued.

    ``account_id`` marks the ones that account already has, which is what the
    «Añadir desde la biblioteca» picker of a queue greys out. ``queue`` keeps
    the ones in that queue (its id), in any queue (``any``) or never queued
    (``none``), whatever their status there; hidden queues do not count, and
    ``accounts`` (several) narrows «any» and «none» to those accounts' queues,
    the ones the library page is filtered to. ``text`` keeps the ones whose
    texts are written (``written``), still being written (``writing``) or
    whose writing failed (``failed``). ``folders`` (several) narrowed by
    ``labels``, all of them or any (``match``), keep a combination; ``label`` is
    one label; ``exclude`` leaves out the folders carrying any of its labels and
    ``unlabeled`` adds (``include``) or leaves out (``exclude``) the folders
    without labels. ``q`` searches inside whatever the rest keeps. Every video
    carries the editor's review of its texts (``review``); ``issues`` keeps only
    the ones where it found something to fix.
    """
    stmt = select(LibraryItem)
    if folder:
        stmt = stmt.where(LibraryItem.folder == folder)
    if queue:
        entries = select(VideoItem.id).where(VideoItem.library_item_id == LibraryItem.id)
        if queue in ("any", "none"):
            # A row whose queue was deleted (NULL) is history, and still counts.
            hidden = select(Schedule.id).where(Schedule.hidden.is_(True))
            entries = entries.where(or_(VideoItem.schedule_id.is_(None),
                                        VideoItem.schedule_id.not_in(hidden)))
            if accounts:
                entries = entries.where(VideoItem.account_id.in_(accounts))
        if queue == "none":
            stmt = stmt.where(~entries.exists())
        elif queue == "any":
            stmt = stmt.where(entries.exists())
        elif queue.isdigit():
            stmt = stmt.where(entries.where(VideoItem.schedule_id == int(queue)).exists())
        else:
            raise HTTPException(400, "queue debe ser el id de una cola, «any» o «none».")
    keep = library.resolve_folders(session, folders, [*(labels or []), *([label] if label else [])],
                                   match, exclude, unlabeled)
    if keep is not None:
        stmt = stmt.where(LibraryItem.folder.in_(keep))
    if text:
        if text == "writing":
            stmt = stmt.where(LibraryItem.caption_status.in_(library.WRITING))
        elif text == "failed":
            stmt = stmt.where(LibraryItem.caption_status == "failed")
        elif text == "written":
            stmt = stmt.where(LibraryItem.caption_status.not_in((*library.WRITING, "failed")))
        else:
            raise HTTPException(400, "text debe ser «written», «writing» o «failed».")
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            LibraryItem.original_name.ilike(like), LibraryItem.folder.ilike(like),
            LibraryItem.tiktok_description.ilike(like), LibraryItem.youtube_title.ilike(like),
        ))
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    ordered = stmt.order_by(LibraryItem.folder, LibraryItem.original_name)
    fixed = tuple(captions.stored_hashtags(session))
    if issues:
        # The review is not a column: every candidate is checked, then cut.
        items = [i for i in session.scalars(ordered)
                 if (r := library.review(i, fixed)) and (r["errors"] or r["warnings"])]
        total = len(items)
        items = items[:max(1, min(limit, MAX_LIST))]
    else:
        items = list(session.scalars(ordered.limit(max(1, min(limit, MAX_LIST)))))
    usage = library.usage(session, [i.id for i in items])
    rows = []
    for item in items:
        row = library_json(item, usage.get(item.id, []), _tz())
        row["review"] = library.review(item, fixed)
        if account_id is not None:
            row["in_account"] = any(v["account_id"] == account_id for v in row["queued"])
        rows.append(row)
    return {"total": total, "items": rows}


@router.get("/library/{item_id}")
def library_item(item_id: int, session: Session = Depends(get_session)) -> dict:
    return library_json(_item_or_404(session, item_id), tz_name=_tz())


@router.get("/library/{item_id}/file")
def library_file(item_id: int, session: Session = Depends(get_session)):
    item = _item_or_404(session, item_id)
    path = Path(item.file_path)
    if not library.owns(path):
        raise HTTPException(403, "El archivo está fuera de la biblioteca.")
    if not path.exists():
        raise HTTPException(404, "El archivo ya no existe.")
    return FileResponse(str(path), media_type=publisher.mime_for(path), filename=path.name,
                        content_disposition_type="inline")


@router.get("/library/{item_id}/cover")
def library_cover(item_id: int, session: Session = Depends(get_session)):
    item = _item_or_404(session, item_id)
    cover = queue.cover_path(library.current_path(item.file_path))
    if cover is None:
        raise HTTPException(404, "Este vídeo no tiene portada.")
    if not library.owns(cover):
        raise HTTPException(403, "La portada está fuera de la biblioteca.")
    return FileResponse(str(cover), media_type=publisher.mime_for(cover), filename=cover.name,
                        content_disposition_type="inline")


# --------------------------------------------------------------------------- #
# Uploading
# --------------------------------------------------------------------------- #

def _text_key(name: str) -> str:
    """What pairs a .txt with its video: the file name, sans folders and extension."""
    return Path(Path(name).name).stem.lower()


@router.post("/library/upload")
async def upload(
    files: list[UploadFile] = File(...),
    folder: str = Form(""),
    into: str = Form(""),
    ai: str = Form(""),
    session: Session = Depends(get_session),
) -> dict:
    """Receive videos (with their .txt and their cover) into the library.

    The UI sends a whole folder one video at a time, each with its .txt and
    its cover (``<video>.jpg`` or ``<video>_portada.jpg``), so one request
    never carries gigabytes. The folder of each video is the
    series in its name; ``folder`` (the name of the uploaded directory) is
    only the fallback for names that do not say. ``into`` overrides both:
    the folder (existing or new) the user picked for everything uploaded
    together; a video already in the library stays where it is. ``ai``
    («1»/«0») is the switch «Reescribir los textos con IA» as the page
    showed it when the upload started; empty, the stored one.
    """
    forced = library.slug(into)
    if into.strip() and not forced:
        raise HTTPException(422, "Escribe un nombre de carpeta con alguna letra o número.")
    settings = get_settings()
    rewrite = {"1": True, "true": True, "0": False, "false": False}.get(ai.strip().lower())
    lib_root = library.root(settings)
    added, duplicates, problems, notes = [], [], [], []

    texts: dict[str, tuple[str, str]] = {}
    covers: dict[str, tuple[str, bytes]] = {}
    videos: list[UploadFile] = []
    for upload_file in files:
        name = Path(upload_file.filename or "video.mp4").name
        if Path(name).suffix.lower() in queue.COVER_SUFFIXES:
            raw = await upload_file.read()
            if len(raw) > MAX_COVER_BYTES:
                problems.append(f"«{name}» es demasiado grande para ser una portada.")
                continue
            covers[queue.cover_key(name)] = (name, raw)
            continue
        if Path(name).suffix.lower() != ".txt":
            videos.append(upload_file)
            continue
        raw = await upload_file.read()
        if len(raw) > MAX_SIDECAR_BYTES:
            problems.append(f"«{name}» es demasiado grande para ser un texto.")
            continue
        texts[_text_key(name)] = (name, raw.decode("utf-8", errors="replace"))

    for upload_file in videos:
        name = Path(upload_file.filename or "video.mp4").name
        if Path(name).suffix.lower() not in media.SUPPORTED_SUFFIXES:
            problems.append(f"«{name}»: sólo se admiten vídeos MP4, MOV o WebM, sus .txt y "
                            "su portada .jpg o .png.")
            continue
        # Stream to disk in a thread: the file may be gigabytes, and neither
        # RAM nor the event loop should pay for that.
        incoming = await asyncio.to_thread(library.store_upload, name, upload_file.file, lib_root)
        if incoming.stat().st_size > MAX_UPLOAD_BYTES:
            incoming.unlink(missing_ok=True)
            problems.append(f"«{name}» supera los 4 GB.")
            continue
        text = texts.pop(_text_key(name), None)
        cover = covers.pop(_text_key(name), None)
        digest = await asyncio.to_thread(media.checksum, incoming)
        existing = library.by_checksum(session, digest)
        if existing is not None:
            incoming.unlink(missing_ok=True)
            duplicates.append({"name": name, "library_id": existing.id,
                               "folder": existing.folder,
                               "original_name": existing.original_name})
            if text is not None and not Path(existing.file_path).with_suffix(".txt").exists():
                notes.append(library.attach_sidecar(session, existing, text[1], text[0], ai=rewrite))
            if cover is not None and queue.cover_path(Path(existing.file_path)) is None:
                notes.append(library.attach_cover(existing, cover[1], cover[0]))
            continue
        target = await asyncio.to_thread(
            library.settle, incoming, name, forced or library.destination(session, name, folder),
            lib_root)
        if text is not None:
            # Under the name the video ended up with (it may have become "-1").
            queue.write_sidecar(target, text[1])
        if cover is not None:
            queue.write_cover(target, cover[1], Path(cover[0]).suffix)
        result = library.register(session, target, folder=target.parent.name, checksum=digest, ai=rewrite)
        if result.item is None:
            problems.extend(f"«{name}»: {p}" for p in result.problems)
            continue
        added.append(library_json(result.item, [], _tz()))
        problems.extend(f"«{name}»: {p}" for p in result.problems)

    # A .txt uploaded on its own finds its video already in the library.
    for _key, (name, text) in texts.items():
        item = library.named(session, name)
        if item is None:
            problems.append(f"«{name}»: no hay ningún vídeo con ese nombre en la biblioteca; "
                            "se ignora.")
            continue
        notes.append(library.attach_sidecar(session, item, text, name, ai=rewrite))
        events.record(session, "sidecar_attached", notes[-1])
    # And so does a cover.
    for _key, (name, data) in covers.items():
        item = library.named(session, _key)
        if item is None:
            problems.append(f"«{name}»: no hay ningún vídeo con ese nombre en la biblioteca; "
                            "se ignora.")
            continue
        notes.append(library.attach_cover(item, data, name))
        events.record(session, "cover_attached", f"Biblioteca · {notes[-1]}")

    session.commit()
    return {"added": added, "duplicates": duplicates, "problems": problems, "notes": notes}


@router.post("/library/scan")
def scan(session: Session = Depends(get_session)) -> dict:
    """Register now what was copied into media/library by hand."""
    added = library.scan(session, library.root())
    session.commit()
    return {"added": len(added), "names": [i.original_name for i in added]}


@router.post("/library/folders/rename")
def rename_folder(body: FolderRenameIn, session: Session = Depends(get_session)) -> dict:
    """Rename a folder: the directory in media/library and every row that names it."""
    try:
        result = library.rename_folder(session, body.old, body.new)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"No se pudo renombrar la carpeta en disco: {exc}") from exc
    return {**result, "message": f"«{result['old']}» se llama ahora «{result['folder']}»"
                                 f" ({result['items']} vídeo(s))."}


@router.post("/library/move")
def move(body: LibraryMoveIn, session: Session = Depends(get_session)) -> dict:
    """Move videos into another folder, with their .txt and cover."""
    items = list(session.scalars(select(LibraryItem).where(LibraryItem.id.in_(body.ids))
                                 .order_by(LibraryItem.id)))
    try:
        result = library.move_items(session, items, body.folder)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    message = f"{result['moved']} vídeo(s) movidos a «{result['folder']}»"
    if result["problems"]:
        message += f"; {len(result['problems'])} no"
    return {**result, "message": message + "."}


@router.post("/library/folders/labels")
def folder_labels(body: FolderLabelsIn, session: Session = Depends(get_session)) -> dict:
    """Replace the labels of a folder (they follow it when it is renamed)."""
    try:
        labels = library.set_folder_labels(session, body.folder, body.labels)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    session.commit()
    return {"folder": body.folder, "labels": labels}


@router.post("/library/labels/color")
def label_color(body: LabelColorIn, session: Session = Depends(get_session)) -> dict:
    """The colour of a label, the same on every folder that carries it."""
    try:
        color = library.set_label_color(session, body.label, body.color)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    session.commit()
    return {"label": body.label.strip().lower(), "color": color}


# --------------------------------------------------------------------------- #
# Editing
# --------------------------------------------------------------------------- #

@router.patch("/library/{item_id}")
def patch_item(item_id: int, body: LibraryPatch, session: Session = Depends(get_session)) -> dict:
    """Edit the texts of one or both platforms.

    The user's words are final: anything being generated is dropped, and the
    queues still following this video get the new texts.
    """
    item = _item_or_404(session, item_id)
    touched = False
    for platform in library.PLATFORMS:
        texts = getattr(body, platform)
        if texts is None:
            continue
        library.set_texts(item, platform, **texts.model_dump(exclude_none=True))
        touched = True
    if touched:
        item.caption_status = "none" if item.caption_status in library.WRITING \
            else item.caption_status
        item.caption_source = "manual"
        item.caption_error = None
        followers = library.propagate(session, item)
        events.record(session, "library_edited",
                      f"Biblioteca · textos de «{item.original_name}» editados"
                      + (f" · actualizados en {followers} vídeo(s) en cola" if followers else ""))
    session.commit()
    session.refresh(item)
    return library_json(item, tz_name=_tz())


def _rewrite(session: Session, item: LibraryItem, *, urgent: bool = False) -> bool:
    if not Path(item.file_path).exists() or item.preflight_errors:
        return False
    library.request_rewrite(item, urgent=urgent)
    library.propagate(session, item)
    return True


@router.post("/library/{item_id}/rewrite")
def rewrite(item_id: int, session: Session = Depends(get_session)) -> dict:
    """Listen to the video again and write every platform's texts anew."""
    if not get_settings().caption_ai_enabled:
        raise HTTPException(400, "Los textos automáticos están desactivados "
                                 "(CAPTION_AI_ENABLED=false en .env).")
    item = _item_or_404(session, item_id)
    # One video asked for by hand: ahead of whatever folder is being written.
    if not _rewrite(session, item, urgent=True):
        raise HTTPException(400, "El archivo no existe o no pasa la validación.")
    events.record(session, "library_rewrite",
                  f"Biblioteca · textos de «{item.original_name}» pedidos de nuevo.")
    session.commit()
    return library_json(item, tz_name=_tz())


@router.post("/library/bulk")
def bulk(body: LibraryBulkIn, session: Session = Depends(get_session)) -> dict:
    """«Reescribir», «Textos de la ficha» or «Borrar» for a selection."""
    items = list(session.scalars(select(LibraryItem).where(LibraryItem.id.in_(body.ids))))
    done, problems = 0, []
    if body.action == "sheet":
        for item in items:
            sheet = queue.read_sheet(Path(item.file_path))
            if sheet is None:
                problems.append(f"«{item.original_name}»: no tiene ficha .txt de ContentBuilder al lado.")
                continue
            if item.caption_status == "generating":
                problems.append(f"«{item.original_name}»: se está escribiendo ahora; repítelo al acabar.")
                continue
            used, note = library.apply_sheet(session, item, sheet)
            library.propagate(session, item)
            if used:
                done += 1
            else:
                problems.append(f"«{item.original_name}»: {note}.")
        if done:
            events.record(session, "library_sheet_texts",
                          f"Biblioteca · {done} vídeo(s) con los textos de su ficha .txt.")
    elif body.action == "rewrite":
        if not get_settings().caption_ai_enabled:
            raise HTTPException(400, "Los textos automáticos están desactivados "
                                     "(CAPTION_AI_ENABLED=false en .env).")
        for item in items:
            if _rewrite(session, item):
                done += 1
            else:
                problems.append(f"«{item.original_name}»: no existe o no pasa la validación.")
        if done:
            events.record(session, "library_rewrite",
                          f"Biblioteca · {done} vídeo(s) vuelven a escribir sus textos.")
    else:
        for item in items:
            try:
                library.delete(session, item, delete_file=body.delete_files)
                done += 1
            except ValueError as exc:
                problems.append(str(exc))
    session.commit()
    return {"done": done, "problems": problems}


@router.post("/library/tags")
def bulk_tags(body: LibraryTagsIn, session: Session = Depends(get_session)) -> dict:
    """Add or remove hashtags on many videos at once (a selection, or a whole folder).

    Videos whose texts are still being written are left alone: the captioner
    would overwrite the change a moment later. Queues following a changed
    video get the new list, as with any edit in the library.
    """
    if not body.add and not body.remove:
        raise HTTPException(400, "Di qué hashtags añadir o quitar.")
    if not body.ids and not body.folder:
        raise HTTPException(400, "Elige vídeos o una carpeta.")
    conditions = []
    if body.ids:
        conditions.append(LibraryItem.id.in_(body.ids))
    if body.folder:
        conditions.append(LibraryItem.folder == body.folder)
    items = list(session.scalars(select(LibraryItem).where(or_(*conditions))))
    changed, writing, followers = 0, [], 0
    for item in items:
        if item.caption_status in library.WRITING:
            writing.append(item.original_name)
            continue
        touched = [library.edit_tags(item, platform, add=body.add, remove=body.remove,
                                     position=body.position) for platform in body.platforms]
        if not any(touched):
            continue
        item.caption_source = "manual"
        item.caption_error = None
        followers += library.propagate(session, item)
        changed += 1
    what = " · ".join(filter(None, [
        "añadidos " + " ".join(hashtags.normalize_all(body.add)) if body.add else "",
        "quitados " + " ".join(hashtags.normalize_all(body.remove)) if body.remove else "",
    ]))
    if changed:
        events.record(session, "library_tags_edited",
                      f"Biblioteca · hashtags de {changed} vídeo(s): {what}"
                      + (f" · llegan a {followers} vídeo(s) en cola" if followers else ""))
    session.commit()
    message = f"{changed} vídeo(s) cambiados"
    if len(items) - changed - len(writing):
        message += f", {len(items) - changed - len(writing)} ya estaban así"
    if writing:
        message += f"; {len(writing)} se están escribiendo y no se tocaron (repítelo al acabar)"
    return {"changed": changed, "writing": writing[:20], "followers": followers,
            "total": len(items), "message": message + "."}


@router.delete("/library/{item_id}")
def delete_item(item_id: int, delete_file: bool = True,
                session: Session = Depends(get_session)) -> dict:
    item = _item_or_404(session, item_id)
    try:
        result = library.delete(session, item, delete_file=delete_file)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    session.commit()
    return {"ok": True, **result}


# --------------------------------------------------------------------------- #
# Into a queue
# --------------------------------------------------------------------------- #

@router.post("/schedules/{schedule_id}/videos")
def enqueue(schedule_id: int, body: EnqueueIn, session: Session = Depends(get_session)) -> dict:
    """Add library videos to a queue, with the texts already written for its platform."""
    schedule = session.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(404, "Cola no encontrada")
    wanted = list(dict.fromkeys(body.library_ids))
    if body.folder:
        wanted += [i for i in session.scalars(
            select(LibraryItem.id).where(LibraryItem.folder == body.folder)
            .order_by(LibraryItem.original_name)
        ) if i not in wanted]
    if not wanted:
        raise HTTPException(400, "Elige al menos un vídeo de la biblioteca.")
    if body.publish_now and body.publish_at is not None:
        raise HTTPException(400, "Elige publicar ya o programar una hora, no las dos cosas.")
    if body.publish_at is not None and body.publish_at <= datetime.now(timezone.utc):
        raise HTTPException(400, "Esa hora ya pasó: elige una más tarde.")
    if (body.publish_now or body.publish_at is not None) and not schedule.account.is_active:
        what = "publicar ya" if body.publish_now else "programar en Zernio"
        raise HTTPException(409, f"La cuenta de «{schedule.name}» está desactivada: actívala en "
                                 f"Cuentas para {what}.")
    found = {i.id: i for i in session.scalars(select(LibraryItem).where(LibraryItem.id.in_(wanted)))}

    added, problems = [], []
    for item_id in wanted:
        item = found.get(item_id)
        if item is None:
            problems.append(f"El vídeo {item_id} ya no está en la biblioteca.")
            continue
        video, why = library.enqueue(session, item, schedule, rewrite=body.rewrite)
        if video is None:
            problems.append(why)
            continue
        added.append(video)
        if body.publish_now:
            queue.send_now(video, extra=True)
            events.record(
                session, "publish_now",
                f"Publicación inmediata, fuera de turno, en «{schedule.name}»: «{video.original_name}»",
                account_id=schedule.account_id, video_id=video.id,
            )
        elif body.publish_at is not None:
            delegation.delegate_at(video, body.publish_at)
            events.record(
                session, "publish_at",
                f"Programado en Zernio, fuera de turno, en «{schedule.name}» para el "
                f"{scheduling.local_label(body.publish_at, schedule.timezone)}: «{video.original_name}»",
                account_id=schedule.account_id, video_id=video.id,
            )
    scheduling.plan_next_slot(session, schedule)
    session.commit()
    message = f"{len(added)} vídeo(s) añadidos a «{schedule.name}»"
    if body.publish_now and added:
        message = f"{len(added)} vídeo(s) saliendo ya en «{schedule.name}», fuera de turno"
    elif body.publish_at is not None and added:
        message = (f"{len(added)} vídeo(s) de «{schedule.name}» programados en Zernio para el "
                   f"{scheduling.local_label(body.publish_at, schedule.timezone)}")
    if (body.publish_now or body.publish_at is not None) and any(
            v.caption_status in ("pending", "generating") for v in added):
        message += (" (en cuanto termine de escribirse su texto)" if body.publish_now
                    else " (se sube a Zernio en cuanto termine de escribirse su texto)")
    return {
        "added": [video_json(v, schedule.timezone) for v in added],
        "problems": problems,
        "message": message + (f" · {len(problems)} no" if problems else ""),
    }
