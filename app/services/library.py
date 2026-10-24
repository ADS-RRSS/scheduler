"""The shared library: every video once, with its texts for each platform.

    media/library/<series>/reel_07_<series>_<topic>.mp4   the video
    media/library/<series>/reel_07_<series>_<topic>.txt   its caption or reel sheet
    media/library/<series>/reel_07_<series>_<topic>.jpg   its cover (YouTube thumbnail, TikTok cover)

A reel published on TikTok and on YouTube used to be two uploads, two copies
on disk and two rounds of Whisper and Ollama. Now it enters the library once:
it is validated there, its texts are written there — a title, a description,
hashtags and a comment to pin, for TikTok and for YouTube — and a queue takes
it from there, copying the texts of its account's platform. Nothing is
processed again at that point unless the user asks for it («Reescribir para
esta cola», or «Regenerar» on the queued video).

A queued video keeps following its library item (``caption_source`` =
:data:`FROM_LIBRARY`) until someone edits or regenerates it in the queue:
a text finished, rewritten or corrected in the library reaches every queue
that has not taken its own, as long as the video has not left for Zernio.

The folder is the series slug of the file name, because that is how the
reel generator names them: ``reel_01_microcomportamientos-psicologicos-que-
delatan_lo-que-revela-el-reencuentro`` lives in
``microcomportamientos-psicologicos-que-delatan``.

The library folder is also the watch folder: a video copied in by hand (with
its .txt) is registered on the next tick of the worker.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time as _time
import unicodedata
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import Account, LibraryItem, Schedule, Setting, VideoItem, VideoStatus
from . import autocaption, captions, events, hashtags, media, queue, reelinfo, textcheck

log = logging.getLogger("scheduler")

PLATFORMS = ("tiktok", "youtube")
PLATFORM_LABELS = {"tiktok": "TikTok", "youtube": "YouTube"}
TEXT_FIELDS = ("title", "description", "tags", "first_comment")
# VideoItem.caption_source of a queued video whose texts are the library's.
FROM_LIBRARY = "biblioteca"
WRITING = ("pending", "generating")
# Uploads land here first: a file is only moved into its folder once it is
# known not to be a duplicate, so a half-received upload never shows up.
INCOMING_DIR = ".incoming"
# Where a file whose name says nothing about its series goes.
DEFAULT_FOLDER = "varios"
# A video that is still in a queue (or in Zernio, waiting for its hour) needs
# its file: the library refuses to delete it until it is published or failed.
FINISHED = (VideoStatus.PUBLISHED, VideoStatus.FAILED)
# settings row: series slug (or old folder name) -> the folder it was renamed to.
ALIASES_KEY = "library_folder_aliases"
# settings row: folder -> the labels the user gave it («serie», «terminada»…).
LABELS_KEY = "library_folder_labels"
# settings row: label -> its colour (#rrggbb) when the user picked one.
LABEL_COLORS_KEY = "library_label_colors"
# settings row: the library's switch «Reescribir los textos con IA». Off: a
# ContentBuilder sheet's texts are published as they come, without listening to
# the video or calling Ollama.
AI_TEXTS_KEY = "library_ai_texts"
MAX_LABELS = 12
_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")
MAX_LABEL_CHARS = 32

_SERIES_RE = re.compile(r"^reel_\d+_([^_]+)_", re.IGNORECASE)
_TAG_RE = re.compile(r"#[^\s#]+")


# --------------------------------------------------------------------------- #
# Where things live
# --------------------------------------------------------------------------- #

def root(settings: Settings | None = None) -> Path:
    return Path((settings or get_settings()).media_library_dir)


def slug(text: str, limit: int = 120) -> str:
    """ASCII, lower-case, dashes: what a folder name may contain."""
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    cleaned = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")
    return cleaned[:limit].strip("-")


def folder_for(name: str, hint: str = "") -> str:
    """The library folder of a file: the series in its name.

    ``reel_002_pensamientos-estoicos_el-juicio-no-el-hecho.mp4`` goes to
    ``pensamientos-estoicos``. A name without a series goes to the folder it
    was uploaded from (``hint``), or to :data:`DEFAULT_FOLDER`.
    """
    match = _SERIES_RE.match(Path(name).stem)
    if match and slug(match.group(1)):
        return slug(match.group(1))
    return slug(hint) or DEFAULT_FOLDER


def aliases(session: Session) -> dict[str, str]:
    """Folders renamed by the user: what a file's name says -> where it goes now."""
    row = session.get(Setting, ALIASES_KEY)
    value = row.value if row is not None else None
    return dict(value) if isinstance(value, dict) else {}


def destination(session: Session, name: str, hint: str = "") -> str:
    """:func:`folder_for`, after the user's renames.

    Once ``pensamientos-estoicos`` is renamed to ``estoicos``, the next
    ``reel_NN_pensamientos-estoicos_…`` goes to ``estoicos`` too instead of
    bringing the old folder back.
    """
    folder = folder_for(name, hint)
    return aliases(session).get(folder, folder)


def folder_labels(session: Session) -> dict[str, list[str]]:
    """The labels of every folder that has any."""
    row = session.get(Setting, LABELS_KEY)
    value = row.value if row is not None else None
    if not isinstance(value, dict):
        return {}
    return {folder: [str(x) for x in labels] for folder, labels in value.items()
            if isinstance(labels, list) and labels}


def clean_labels(raw: list[str]) -> list[str]:
    """Trimmed, single-spaced, lower-case, without a leading «#», each once."""
    out: list[str] = []
    for text in raw:
        label = re.sub(r"\s+", " ", str(text or "")).strip().lstrip("#").strip().lower()
        if not label:
            continue
        if len(label) > MAX_LABEL_CHARS:
            raise ValueError(f"«{label[:40]}…» es demasiado larga: hasta {MAX_LABEL_CHARS} caracteres.")
        if label not in out:
            out.append(label)
    if len(out) > MAX_LABELS:
        raise ValueError(f"Hasta {MAX_LABELS} etiquetas por carpeta.")
    return out


def _store_labels(session: Session, table: dict[str, list[str]]) -> None:
    table = {folder: labels for folder, labels in table.items() if labels}
    row = session.get(Setting, LABELS_KEY)
    if row is None:
        session.add(Setting(key=LABELS_KEY, value=table))
    else:
        row.value = table


def set_folder_labels(session: Session, folder: str, labels: list[str]) -> list[str]:
    """Replace the labels of a folder of the library."""
    if not session.scalar(select(func.count(LibraryItem.id)).where(LibraryItem.folder == folder)):
        raise LookupError(f"No hay ninguna carpeta «{folder}» en la biblioteca.")
    cleaned = clean_labels(labels)
    table = folder_labels(session)
    table[folder] = cleaned
    _store_labels(session, table)
    return cleaned


def label_colors(session: Session) -> dict[str, str]:
    """The colours the user picked, by label (the others get one from their name)."""
    row = session.get(Setting, LABEL_COLORS_KEY)
    value = row.value if row is not None else None
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if isinstance(v, str) and _COLOR_RE.match(v)}


def set_label_color(session: Session, label: str, color: str | None) -> str | None:
    """Give a label a colour everywhere it shows, or back to automatic with ``None``."""
    (label,) = clean_labels([label]) or [""]
    if not label:
        raise ValueError("Falta la etiqueta.")
    color = (color or "").strip().lower() or None
    if color is not None and not _COLOR_RE.match(color):
        raise ValueError("El color debe ser #rrggbb.")
    table = label_colors(session)
    if color is None:
        table.pop(label, None)
    else:
        table[label] = color
    row = session.get(Setting, LABEL_COLORS_KEY)
    if row is None:
        session.add(Setting(key=LABEL_COLORS_KEY, value=table))
    else:
        row.value = table
    return color


def resolve_folders(session: Session, folders: list[str] | None, labels: list[str] | None,
                    match: str = "all", exclude: list[str] | None = None,
                    unlabeled: str = "") -> list[str] | None:
    """The folders ticked (none = every folder), narrowed by the labels ticked.

    ``match="all"`` keeps the folders carrying every label; ``"any"``, the ones
    carrying at least one. ``unlabeled="include"`` adds the folders without
    any label to those (alone: only those), ``"exclude"`` leaves them out.
    ``exclude`` leaves out the folders carrying any of those labels. ``None``
    when nothing narrows anything (the whole library), which is not the same
    as an empty list (nothing matches).
    """
    def clean(raw: list[str] | None) -> list[str]:
        return [w for w in dict.fromkeys(
            (x or "").strip().lstrip("#").strip().lower() for x in (raw or [])) if w]

    folders = [f for f in (folders or []) if f]
    wanted, unwanted = clean(labels), clean(exclude)
    bare = unlabeled == "include"
    if not folders and not wanted and not unwanted and not unlabeled:
        return None
    base = set(folders) if folders else set(
        session.scalars(select(LibraryItem.folder).distinct()))
    if wanted or unwanted or unlabeled:
        table = folder_labels(session)
        test = any if match == "any" else all

        def keeps(folder: str) -> bool:
            have = table.get(folder, [])
            if any(w in have for w in unwanted) or (unlabeled == "exclude" and not have):
                return False
            if not wanted and not bare:
                return True
            return (bool(wanted) and test(w in have for w in wanted)) or (bare and not have)

        base = {f for f in base if keeps(f)}
    return sorted(base)


def ai_rewrites(session: Session) -> bool:
    """Is the switch «Reescribir los textos con IA» on? Off unless turned on.

    It decides what a video with a ContentBuilder sheet gets: off, the texts the
    sheet brings, as they are; on, texts written anew by Ollama from it. A
    video without a sheet is listened to and written either way: there is
    nothing to read.
    """
    row = session.get(Setting, AI_TEXTS_KEY)
    value = row.value if row is not None else None
    return bool(value.get("value")) if isinstance(value, dict) else bool(value)


def set_ai_rewrites(session: Session, enabled: bool) -> bool:
    row = session.get(Setting, AI_TEXTS_KEY)
    if row is None:
        session.add(Setting(key=AI_TEXTS_KEY, value={"value": bool(enabled)}))
    else:
        row.value = {"value": bool(enabled)}
    return bool(enabled)


def relocated(path: str | Path, settings: Settings | None = None) -> Path:
    """``path``, moved from the old library folder (TikTok Scheduler) to this one.

    Only a path under one of ``legacy_library_dirs`` changes, and only after
    collapsing any ``..``: whatever comes out of an old folder lands inside the
    library, never next to it.
    """
    settings = settings or get_settings()
    path = Path(path)
    normal = Path(os.path.normpath(path))
    for old in settings.legacy_library_dirs:
        old = Path(os.path.normpath(old))
        if normal.is_relative_to(old):
            return root(settings) / normal.relative_to(old)
    return path


def owns(path: str | Path, settings: Settings | None = None) -> bool:
    """Is this file one of the library's (and so shared by every queue)?"""
    try:
        return relocated(path, settings).resolve().is_relative_to(root(settings).resolve())
    except OSError:
        return False


def platform_of(account: Account | None) -> str:
    platform = (account.platform if account is not None else "") or "tiktok"
    return platform if platform in PLATFORMS else "tiktok"


# --------------------------------------------------------------------------- #
# Texts
# --------------------------------------------------------------------------- #

def texts_of(item: LibraryItem, platform: str) -> dict:
    """The four texts of one platform, as the API and the queue use them."""
    platform = platform if platform in PLATFORMS else "tiktok"
    return {
        "title": getattr(item, f"{platform}_title") or "",
        "description": getattr(item, f"{platform}_description") or "",
        "tags": list(getattr(item, f"{platform}_tags") or []),
        "first_comment": getattr(item, f"{platform}_first_comment") or "",
    }


def set_texts(item: LibraryItem, platform: str, *, title: str | None = None,
              description: str | None = None, tags: list[str] | None = None,
              first_comment: str | None = None) -> None:
    """Overwrite whichever of the four texts is given (None = leave it)."""
    if platform not in PLATFORMS:
        raise ValueError(f"plataforma desconocida: {platform}")
    if title is not None:
        setattr(item, f"{platform}_title", title.strip()[:autocaption.TITLE_MAX_CHARS])
    if description is not None:
        setattr(item, f"{platform}_description", description.strip())
    if tags is not None:
        setattr(item, f"{platform}_tags", hashtags.normalize_all(tags) or None)
    if first_comment is not None:
        setattr(item, f"{platform}_first_comment", first_comment.strip())


def set_all(item: LibraryItem, **texts) -> None:
    """The same texts for every platform."""
    for platform in PLATFORMS:
        set_texts(item, platform, **texts)


def edit_tags(item: LibraryItem, platform: str, *, add=(), remove=(),
              position: str = "start") -> bool:
    """Add and remove hashtags on one platform's list. True when it changed.

    Added ones go first by default: TikTok counts the first five, and a tag
    put on a whole folder is usually one that has to count.
    """
    current = texts_of(item, platform)["tags"]
    gone = {hashtags.fold(t) for t in hashtags.normalize_all(remove)}
    kept = [t for t in current if hashtags.fold(t) not in gone]
    present = {hashtags.fold(t) for t in kept}
    new = [t for t in hashtags.normalize_all(add) if hashtags.fold(t) not in present]
    tags = new + kept if position == "start" else kept + new
    if tags == current:
        return False
    set_texts(item, platform, tags=tags)
    return True


# What the review of a library video said, by (id, last change, fixed tags):
# the list is drawn every few seconds while a folder is being written.
_reviews: dict[tuple, dict] = {}
_REVIEWS_MAX = 5000


def review(item: LibraryItem, fixed: tuple[str, ...] = ()) -> dict | None:
    """The editor's checks on both platforms' texts, summed up for the list.

    None while the texts are being written (there is nothing to review yet)
    or when the file does not pass the validation (it cannot be queued).
    Tips are left out of the counts: they are suggestions, not problems.
    """
    if item.caption_status in WRITING or item.preflight_errors:
        return None
    key = (item.id, item.updated_at, item.caption_status, tuple(fixed))
    cached = _reviews.get(key)
    if cached is not None:
        return cached
    ctx = textcheck.Context(fixed=tuple(fixed), source=item.transcript or "",
                            file_name=item.original_name)
    found = []
    for platform in PLATFORMS:
        texts = textcheck.Texts.of(**texts_of(item, platform))
        for issue in textcheck.check(texts, platform, ctx).issues:
            if issue.level in ("error", "warn"):
                found.append({"platform": platform, "level": issue.level, "code": issue.code,
                              "message": issue.message})
    result = {
        "errors": sum(i["level"] == "error" for i in found),
        "warnings": sum(i["level"] == "warn" for i in found),
        "issues": found[:8],
    }
    if len(_reviews) >= _REVIEWS_MAX:
        _reviews.clear()
    _reviews[key] = result
    return result


def title_from(text: str) -> str:
    """A title for a video that came with a hand-written caption: its first line."""
    first = next((line.strip() for line in (text or "").splitlines() if line.strip()), "")
    first = re.sub(r"\s+", " ", _TAG_RE.sub("", first)).strip(" ·-")
    return autocaption._shorten(first, autocaption.TITLE_MAX_CHARS) if first else ""


def sheet_texts(session: Session, sheet: reelinfo.ReelInfo, file_name: str = "") -> dict[str, dict]:
    """The texts of each platform as a sheet gives them, ready to publish.

    A format-2 sheet brings them per platform, written by ContentBuilder for
    each one; a format-1 sheet only a title, its questions and its hashtags
    (:func:`autocaption.sheet_fallback`). Either way the fixed hashtags of
    Ajustes go first and the list stops at HASHTAG_MAX, as everywhere.
    """
    settings = get_settings()
    fixed = captions.stored_hashtags(session)
    limit = min(settings.hashtag_max, hashtags.TIKTOK_MAX_TAGS)
    fallback = None
    out: dict[str, dict] = {}
    for platform in PLATFORMS:
        own = sheet.texts_for(platform)
        if own is None:
            fallback = fallback or autocaption.sheet_fallback(sheet)
            title, description = fallback.title, fallback.caption
            tags, comment = list(fallback.hashtags), fallback.first_comment
        else:
            title, description, tags, comment = own.title, own.description, list(own.tags), own.first_comment
        out[platform] = {
            "title": (title or autocaption.topic_from_filename(file_name))[:autocaption.TITLE_MAX_CHARS],
            "description": description,
            "tags": hashtags.select_tags(fixed=fixed, niche=tags, transcript="", bank=hashtags.DEFAULT_BANK,
                                         min_topic=0, limit=limit),
            "first_comment": comment,
        }
    return out


def apply_sheet(session: Session, item: LibraryItem, sheet: reelinfo.ReelInfo) -> tuple[bool, str]:
    """Give a library video the texts of its sheet, as they come. ``(used, note)``.

    Nothing listens to the video: the duration ffprobe measured must match the
    one the sheet says (format 2), which tells a reel from another of its
    series for free. A sheet that does not match is not used and the video is
    marked failed, with why, so it shows under «Fallidos».
    """
    match, note = reelinfo.duration_match(sheet, (item.probe or {}).get("duration_sec"))
    if match is False:
        item.caption_status, item.caption_source = "failed", "ficha"
        item.caption_error = (f"{note}: no se usó. Comprueba que el .txt sea el suyo, o pulsa «Reescribir» "
                              "para escribirlos desde el audio")
        item.caption_priority = 0
        return False, note
    for platform, texts in sheet_texts(session, sheet, item.original_name).items():
        set_texts(item, platform, **texts)
    notes = [note]
    if not sheet.has_texts:
        notes.append("ficha antigua, sin textos de TikTok ni YouTube: título, pregunta y hashtags de la "
                     "ficha (con «Reescribir los textos con IA» se escriben de nuevo)")
    if sheet.needs_review:
        notes.append("ContentBuilder pide revisar el guion a mano"
                     + (f": {sheet.verdict_notes}" if sheet.verdict_notes else ""))
    item.transcript = sheet.script or item.transcript
    item.caption_status, item.caption_source = "done", "ficha"
    item.caption_error = "; ".join(n for n in notes if n) or None
    item.caption_generated_at = datetime.now(timezone.utc)
    item.caption_priority = 0
    return True, note


def _initial_texts(session: Session, path: Path, errors: list[str], *, ai: bool | None = None) -> dict:
    """What a video has to say the moment it enters, before any model runs.

    A hand-written .txt is final: it is the description on every platform.
    A reel sheet is read as it comes («sheet»: the caller applies it once the
    video is probed) unless the switch asks for AI texts; then its texts (or
    its title and first question) stand in while the captioner writes, so a
    video that has to go out before that finishes still carries something.
    """
    text = queue.sidecar_caption(path)
    if text.strip():
        return {"status": "none", "source": "manual",
                "texts": {"title": title_from(text), "description": text.strip(),
                          "tags": [], "first_comment": ""}}
    sheet = queue.read_sheet(path)
    writes = not errors and get_settings().caption_ai_enabled
    texts = {"title": autocaption.topic_from_filename(path.name)[:autocaption.TITLE_MAX_CHARS],
             "description": "", "tags": [], "first_comment": ""}
    if sheet is not None:
        ai = ai_rewrites(session) if ai is None else ai
        if not (writes and ai):
            return {"sheet": sheet}
        return {"status": "pending", "source": None, "platforms": sheet_texts(session, sheet, path.name)}
    if writes:
        return {"status": "pending", "source": None, "texts": texts}
    return {"status": "none", "source": None, "texts": texts}


# --------------------------------------------------------------------------- #
# Entering the library
# --------------------------------------------------------------------------- #

@dataclass
class Registered:
    item: LibraryItem | None
    problems: list[str]
    # Set when the same bytes are already in the library.
    duplicate_of: LibraryItem | None = None


def by_checksum(session: Session, digest: str) -> LibraryItem | None:
    return session.scalars(select(LibraryItem).where(LibraryItem.checksum == digest)).first()


def register(
    session: Session,
    path: str | Path,
    *,
    folder: str | None = None,
    checksum: str | None = None,
    record_event: bool = True,
    ai: bool | None = None,
) -> Registered:
    """Add a file that already sits in the library to the ``library_items`` table.

    The same bytes under another name are refused (and left alone: the caller
    decides whether to delete them). An invalid file is registered anyway,
    with its errors, so the library can say why it cannot be queued. ``ai``
    is the switch «Reescribir los textos con IA» for this upload (None: the
    stored one).
    """
    path = Path(path)
    if not path.exists():
        return Registered(None, ["El archivo no existe."])
    digest = checksum or media.checksum(path)
    existing = by_checksum(session, digest)
    if existing is not None:
        return Registered(None, [f"Ya está en la biblioteca como «{existing.original_name}» "
                                 f"(carpeta {existing.folder})."], existing)

    check = media.preflight(path)
    errors, warnings = check.errors, check.warnings
    initial = _initial_texts(session, path, errors, ai=ai)
    item = LibraryItem(
        folder=folder or path.parent.name or DEFAULT_FOLDER,
        file_path=str(path),
        original_name=path.name,
        checksum=digest,
        size_bytes=path.stat().st_size,
        probe=check.probe.as_dict(),
        preflight_errors=errors or None,
        preflight_warnings=warnings or None,
        caption_status=initial.get("status", "none"),
        caption_source=initial.get("source"),
        caption_requested_at=(datetime.now(timezone.utc)
                              if initial.get("status") == "pending" else None),
    )
    how = " · con texto propio" if initial.get("source") == "manual" else ""
    if initial.get("sheet") is not None:
        used, _ = apply_sheet(session, item, initial["sheet"])
        how = (" · textos de su ficha .txt" if used else " · su ficha .txt no es de este vídeo")
    elif initial.get("platforms"):
        for platform, texts in initial["platforms"].items():
            set_texts(item, platform, **texts)
        how = " · ficha .txt: Ollama reescribe sus textos"
    else:
        set_all(item, **initial["texts"])
    session.add(item)
    session.flush()
    if record_event:
        if errors:
            events.record(session, "library_invalid",
                          f"Biblioteca · «{path.name}» no pasa la validación: {' '.join(errors)}",
                          level="error")
        else:
            events.record(
                session, "library_added",
                f"Biblioteca · «{path.name}» en {item.folder}{how}",
                level="warn" if item.caption_status == "failed" else "info",
                data={"warnings": warnings} if warnings else None,
            )
    return Registered(item, errors + warnings)


def store_upload(filename: str, source: bytes | BinaryIO, library_root: Path) -> Path:
    """Stream an uploaded file into the library's incoming folder.

    Chunked, so a 3 GB upload never has to fit in RAM, and without database
    access, so it is safe to run in a thread.
    """
    incoming = Path(library_root) / INCOMING_DIR
    target = queue.unique_path(incoming, queue.safe_filename(filename))
    if isinstance(source, (bytes, bytearray)):
        target.write_bytes(source)
    else:
        source.seek(0)
        with target.open("wb") as fh:
            shutil.copyfileobj(source, fh, 8 * 1024 * 1024)
    return target


def clear_incoming(library_root: Path | None = None) -> int:
    """Drop what an interrupted upload left half-written. Only at start-up."""
    incoming = Path(library_root or root()) / INCOMING_DIR
    removed = 0
    if incoming.exists():
        for leftover in incoming.iterdir():
            if leftover.is_file():
                leftover.unlink(missing_ok=True)
                removed += 1
    return removed


def settle(incoming: Path, filename: str, folder: str, library_root: Path) -> Path:
    """Move an accepted upload from the incoming folder into its own folder."""
    target = queue.unique_path(Path(library_root) / folder, queue.safe_filename(filename))
    shutil.move(str(incoming), str(target))
    return target


def attach_sidecar(session: Session, item: LibraryItem, text: str, name: str = "", *,
                   ai: bool | None = None) -> str:
    """Save a .txt next to a library video and act on it. Returns what happened.

    A caption written by hand becomes the description everywhere and wins
    over anything generated; a reel sheet gives the video its texts (or, with
    the switch «Reescribir los textos con IA», asks for them to be written
    again from it), unless the texts are the user's own.
    """
    path = Path(item.file_path)
    queue.write_sidecar(path, text)
    label = f"«{name}»" if name else "El .txt"
    if not reelinfo.is_sheet(text):
        set_all(item, title=title_from(text), description=text.strip(), tags=[],
                first_comment="")
        item.caption_status, item.caption_source, item.caption_error = "none", "manual", None
        propagate(session, item)
        return f"«{item.original_name}» usa ahora el texto de {label}."
    if item.caption_source == "manual" or item.caption_status == "generating":
        return (f"{label} guardado junto a «{item.original_name}» (sus textos son tuyos o se están "
                "escribiendo: pulsa «Textos de la ficha» para usar los de la ficha).")
    ai = ai_rewrites(session) if ai is None else ai
    if ai and get_settings().caption_ai_enabled:
        request_rewrite(item)
        propagate(session, item)
        return f"«{item.original_name}» se reescribirá con IA a partir de la ficha {label}."
    used, note = apply_sheet(session, item, reelinfo.parse(text))
    propagate(session, item)
    if not used:
        return f"«{item.original_name}»: {note}; no se usó la ficha {label}."
    return f"«{item.original_name}» usa ahora los textos de la ficha {label}."


def attach_cover(item: LibraryItem, data: bytes, name: str = "") -> str:
    """Save an image as the cover of a library video. Returns what happened.

    It goes to YouTube as the custom thumbnail and to TikTok as the cover every time a queue
    publishes it from now on; a post already handed to Zernio keeps the one
    it left with.
    """
    replaced = queue.cover_path(Path(item.file_path)) is not None
    queue.write_cover(Path(item.file_path), data, Path(name).suffix or ".jpg")
    label = f"«{name}»" if name else "La imagen"
    return (f"{label} es ahora la portada de «{item.original_name}»"
            + (" (sustituye a la anterior)." if replaced else "."))


def request_rewrite(item: LibraryItem, *, urgent: bool = False) -> None:
    """Ask the captioner for this video's texts again.

    ``urgent`` is for a request made on this one video: it goes ahead of the
    bulk work (a folder of a hundred videos is an hour or two of Ollama).
    """
    item.caption_status = "pending"
    item.caption_requested_at = datetime.now(timezone.utc)
    item.caption_error = None
    item.caption_priority = 1 if urgent else 0


def named(session: Session, stem: str) -> LibraryItem | None:
    """The library video whose file (or original name) is called ``stem``."""
    key = Path(queue.safe_filename(stem)).stem.lower()
    for item in session.scalars(select(LibraryItem).order_by(LibraryItem.id.desc())):
        if Path(item.file_path).stem.lower() == key or \
                Path(queue.safe_filename(item.original_name)).stem.lower() == key:
            return item
    return None


# --------------------------------------------------------------------------- #
# The watch folder
# --------------------------------------------------------------------------- #

# Files found to be duplicates, by (path, size, mtime): not hashed again on
# every tick, and reported once.
_ignored: dict[str, tuple[int, float]] = {}


def scan(session: Session, library_root: Path | None = None,
         min_age_seconds: float = 0) -> list[LibraryItem]:
    """Register the videos copied into the library by hand.

    A file straight in the library's root is first moved into the folder its
    name says, with its .txt and its cover. ``min_age_seconds`` leaves alone what is still
    being copied; the explicit «Buscar ahora» passes 0.
    """
    library_root = Path(library_root or root())
    added: list[LibraryItem] = []
    if not library_root.exists():
        return added
    # Under the lock a rename or a move commits under: a file it just moved is
    # never seen at its new path before the database says it is there.
    with _files_lock:
        known = {Path(p).resolve() for p in session.scalars(select(LibraryItem.file_path))}
        paths = sorted(_candidates(library_root))
    now = _time.time()
    for path in paths:
        resolved = path.resolve()
        if resolved in known:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size == 0 or (min_age_seconds and now - stat.st_mtime < min_age_seconds):
            continue
        if _ignored.get(str(resolved)) == (stat.st_size, stat.st_mtime):
            continue
        if path.parent == library_root:
            path = _file_into_folder(path, library_root, destination(session, path.name))
        result = register(session, path, folder=path.parent.name)
        if result.item is not None:
            added.append(result.item)
        else:
            _ignored[str(path.resolve())] = (stat.st_size, stat.st_mtime)
            events.record(session, "library_duplicate",
                          f"Biblioteca · «{path.name}» no se añade: {' '.join(result.problems)}",
                          level="warn")
    return added


def _candidates(library_root: Path):
    for path in library_root.rglob("*"):
        rel = path.relative_to(library_root).parts
        if any(part.startswith(".") for part in rel):
            continue
        if path.is_file() and path.suffix.lower() in queue.VIDEO_SUFFIXES:
            yield path


def _file_into_folder(path: Path, library_root: Path, folder: str) -> Path:
    target = queue.unique_path(library_root / folder, path.name)
    _move_with_companions(path, target)
    return target


def _companions(path: Path) -> list[Path]:
    """The .txt and the cover sitting next to a video."""
    found = [path.with_suffix(s) for s in queue.SIDECAR_SUFFIXES if path.with_suffix(s).exists()]
    cover = queue.cover_path(path)
    return found + ([cover] if cover is not None else [])


def _move_with_companions(path: Path, target: Path) -> list[tuple[Path, Path]]:
    """Move a video and its companions under ``target``'s name. Returns the moves done."""
    moves = [(path, target)] + [
        (c, target.with_name(target.stem + c.name[len(path.stem):])) for c in _companions(path)]
    done: list[tuple[Path, Path]] = []
    try:
        for src, dst in moves:
            shutil.move(str(src), str(dst))
            done.append((src, dst))
    except OSError:
        _undo_moves(done)
        raise
    return done


def _undo_moves(moves: list[tuple[Path, Path]]) -> None:
    for src, dst in reversed(moves):
        try:
            shutil.move(str(dst), str(src))
        except OSError:
            log.exception("no se pudo devolver %s a %s", dst, src)


# --------------------------------------------------------------------------- #
# From the library to a queue
# --------------------------------------------------------------------------- #

def apply_to_video(video: VideoItem, item: LibraryItem, platform: str | None = None) -> None:
    """Copy the library's texts for the video's platform onto the queued video."""
    texts = texts_of(item, platform or platform_of(video.account))
    video.title = texts["title"]
    video.caption = texts["description"]
    video.auto_hashtags = texts["tags"] or None
    video.first_comment = texts["first_comment"]
    video.caption_source = FROM_LIBRARY
    video.caption_generated_at = item.caption_generated_at
    if item.caption_status in WRITING:
        # "pending" is what the publisher and a delegation wait for; the
        # captioner leaves it alone (own_text) because the library writes it.
        if video.caption_status != "pending":
            video.caption_requested_at = item.caption_requested_at or datetime.now(timezone.utc)
        video.caption_status = "pending"
        video.caption_error = None
    else:
        video.caption_status = item.caption_status or "none"
        video.caption_error = item.caption_error


def followers(session: Session, item: LibraryItem) -> list[VideoItem]:
    """Queued videos still taking their texts from this library item."""
    return list(session.scalars(
        select(VideoItem).where(
            VideoItem.library_item_id == item.id,
            VideoItem.caption_source == FROM_LIBRARY,
            queue.writable(),
        )
    ))


def propagate(session: Session, item: LibraryItem) -> int:
    """Hand the library's current texts to every queued video following it."""
    videos = followers(session, item)
    for video in videos:
        apply_to_video(video, item)
    if videos:
        session.flush()
    return len(videos)


def in_account(session: Session, item: LibraryItem, account_id: int) -> VideoItem | None:
    """This video already entered one of the account's queues (any status)."""
    return session.scalars(
        select(VideoItem).where(
            VideoItem.account_id == account_id,
            (VideoItem.library_item_id == item.id) | (VideoItem.checksum == item.checksum),
        )
    ).first()


def enqueue(
    session: Session, item: LibraryItem, schedule: Schedule, *, rewrite: bool = False,
    priority: int = 100,
) -> tuple[VideoItem | None, str | None]:
    """Put a library video in a queue. Returns ``(video, None)`` or ``(None, why not)``.

    The texts are the library's, for the account's platform, and nothing is
    generated — unless ``rewrite``, which has the captioner write this queue
    its own (from the same sheet, with this account's numbers). The same
    video never enters two queues of one account: it would go out twice.
    """
    if item.preflight_errors:
        return None, f"«{item.original_name}» no pasa la validación: " \
                     + " ".join(item.preflight_errors)
    if not Path(item.file_path).exists():
        return None, f"«{item.original_name}»: el archivo ya no está en la biblioteca."
    existing = in_account(session, item, schedule.account_id)
    if existing is not None:
        where = f" en «{existing.schedule.name}»" if existing.schedule is not None else ""
        return None, f"«{item.original_name}» ya está{where} ({existing.status.value.lower()})."

    video = VideoItem(
        account=schedule.account,
        schedule=schedule,
        library_item=item,
        file_path=item.file_path,
        original_name=item.original_name,
        checksum=item.checksum,
        size_bytes=item.size_bytes,
        probe=item.probe,
        # The AI label is decided here, once, from the queue the video enters:
        # what the queue says later never rewrites what a video went out with,
        # and the user can still flip this one video in the queue view.
        is_aigc=bool(schedule.is_aigc),
        status=VideoStatus.QUEUED,
        priority=priority,
    )
    apply_to_video(video, item, platform_of(schedule.account))
    if video.caption_status == "pending":
        # The queue's patience (CAPTION_MAX_WAIT_MINUTES) starts now, not when
        # the library started writing.
        video.caption_requested_at = datetime.now(timezone.utc)
    if rewrite and get_settings().caption_ai_enabled:
        video.caption_source = None
        video.caption_status = "pending"
        video.caption_requested_at = datetime.now(timezone.utc)
        video.caption_error = None
    session.add(video)
    session.flush()
    events.record(
        session, "video_queued",
        f"Vídeo en cola «{schedule.name}» desde la biblioteca: «{item.original_name}»"
        + (" · se reescribe su texto para esta cola" if video.caption_source is None else ""),
        account_id=schedule.account_id, video_id=video.id,
    )
    return video, None


# --------------------------------------------------------------------------- #
# Reading and removing
# --------------------------------------------------------------------------- #

def usage(session: Session, ids: list[int] | None = None) -> dict[int, list[VideoItem]]:
    """The queue entries of each library item."""
    stmt = select(VideoItem).where(VideoItem.library_item_id.is_not(None))
    if ids is not None:
        stmt = stmt.where(VideoItem.library_item_id.in_(ids))
    out: dict[int, list[VideoItem]] = {}
    for video in session.scalars(stmt.order_by(VideoItem.id)):
        out.setdefault(video.library_item_id, []).append(video)
    return out


def folders(session: Session, account_id: int | None = None) -> list[dict]:
    """One row per folder: how many videos, how many still being written.

    With ``account_id``, also how many that account could still take: valid
    and not in any of its queues yet (what the queue's picker offers).
    """
    rows = session.execute(
        select(
            LibraryItem.folder,
            func.count(LibraryItem.id),
            func.sum(LibraryItem.size_bytes),
            func.sum(LibraryItem.caption_status.in_(WRITING)),
            func.sum(LibraryItem.caption_status == "failed"),
            # JSON: a None may be stored as the text 'null', not as NULL.
            func.sum(func.coalesce(func.json_array_length(LibraryItem.preflight_errors), 0) > 0),
        ).group_by(LibraryItem.folder).order_by(LibraryItem.folder)
    ).all()
    queued = dict(session.execute(
        select(LibraryItem.folder, func.count(func.distinct(LibraryItem.id)))
        .join(VideoItem, VideoItem.library_item_id == LibraryItem.id)
        .group_by(LibraryItem.folder)
    ).all())
    available: dict[str, int] = {}
    if account_id is not None:
        taken = select(VideoItem.id).where(
            VideoItem.account_id == account_id,
            (VideoItem.library_item_id == LibraryItem.id)
            | (VideoItem.checksum == LibraryItem.checksum),
        ).exists()
        available = dict(session.execute(
            select(LibraryItem.folder, func.count(LibraryItem.id))
            .where(~taken, func.coalesce(func.json_array_length(LibraryItem.preflight_errors),
                                         0) == 0)
            .group_by(LibraryItem.folder)
        ).all())
    labels = folder_labels(session)
    return [
        {"folder": folder, "count": count, "bytes": int(size or 0),
         "labels": labels.get(folder, []),
         "writing": int(writing or 0), "failed": int(failed or 0),
         "invalid": int(invalid or 0), "in_queues": int(queued.get(folder, 0)),
         **({"available": int(available.get(folder, 0))} if account_id is not None else {})}
        for folder, count, size, writing, failed, invalid in rows
    ]


def needs_file(video: VideoItem) -> bool:
    """Does this queue entry still need its library file?

    The one rule for «en alguna/ninguna cola» and for refusing a delete:
    anything not published or failed, whatever queue it is in (hidden, of
    another account, or deleted), still has to upload or recover it.
    """
    return video.status not in FINISHED


def blockers(session: Session, item: LibraryItem) -> list[VideoItem]:
    """Queue entries that still need this file."""
    return [v for v in item.videos if needs_file(v)]


_STATUS_WORDS = {
    VideoStatus.QUEUED: "en cola", VideoStatus.SCHEDULED: "programado",
    VideoStatus.UPLOADING: "subiendo", VideoStatus.PROCESSING: "procesando",
    VideoStatus.DELEGATED: "en Zernio",
}


def _blocker_line(video: VideoItem) -> str:
    """«G-TikTok» (@cuenta, programado, oculta): where and why it still needs the file."""
    account = video.account
    who = (account.display_name or f"@{account.username}") if account is not None else ""
    notes = [n for n in (who, _STATUS_WORDS.get(video.status, video.status.value),
                         "oculta" if video.schedule is not None and video.schedule.hidden else "")
             if n]
    where = f"«{video.schedule.name}»" if video.schedule is not None else "sin cola (borrada)"
    return f"{where} ({', '.join(notes)})"


def delete(session: Session, item: LibraryItem, *, delete_file: bool = False) -> dict:
    """Remove a video from the library, and its file too if asked.

    Refused while a queue still needs it — waiting, uploading, or parked in
    Zernio (a recovered post would come back without a file). Published and
    failed entries keep their history; they just stop pointing here.
    """
    busy = blockers(session, item)
    if busy:
        raise ValueError(
            f"«{item.original_name}» sigue pendiente en {len(busy)} cola(s): "
            + "; ".join(sorted({_blocker_line(v) for v in busy}))
            + ". Quítalo de ellas (o recupéralo de Zernio) primero.")
    name, path, folder = item.original_name, Path(item.file_path), item.folder
    for video in list(item.videos):
        video.library_item_id = None
    session.delete(item)
    session.flush()
    removed = False
    if delete_file and path.exists():
        path.unlink(missing_ok=True)
        queue.remove_sidecars(path)
        removed = True
        try:
            if path.parent != root() and not any(path.parent.iterdir()):
                path.parent.rmdir()
        except OSError:
            pass
    events.record(session, "library_removed",
                  f"Biblioteca · «{name}» quitado de {folder}"
                  + (" y borrado del disco" if removed else ""), level="warn")
    return {"file_deleted": removed}


# --------------------------------------------------------------------------- #
# Renaming a folder
# --------------------------------------------------------------------------- #

# A folder is renamed with a single os.rename. A file already open keeps being
# read, but whoever holds the old path and opens it afterwards (an upload about
# to start, Whisper about to listen) would find nothing. Those readers say so
# here, and a rename that would pull a file from under one of them refuses.
_files_lock = threading.RLock()
_reading: Counter[str] = Counter()
# Old folder -> new folder, or old file -> new file (resolved), for a path
# read from the database a moment before a rename or a move was committed.
# Only for the life of the process.
_moved: dict[str, str] = {}


def _key(path: str | Path) -> str:
    return str(Path(path).resolve())


def current_path(path: str | Path) -> Path:
    """``path``, or where it went if its folder was renamed meanwhile.

    A path from the old TikTok Scheduler folder is read under this one.
    """
    path = Path(path)
    if not path.exists():
        path = relocated(path)
    with _files_lock:
        for _ in range(len(_moved)):
            if path.exists():
                break
            # A file moved on its own (:func:`move_items`), or its whole folder.
            moved = _moved.get(_key(path))
            if moved is not None:
                path = Path(moved)
                continue
            moved = _moved.get(_key(path.parent))
            if moved is None:
                break
            path = Path(moved) / path.name
    return path


@contextmanager
def reading(path: str | Path):
    """Hold a library file while it is read; yields its current path."""
    with _files_lock:
        path = current_path(path)
        key = _key(path)
        _reading[key] += 1
    try:
        yield path
    finally:
        with _files_lock:
            _reading[key] -= 1
            if _reading[key] <= 0:
                del _reading[key]


def _read_under(directory: Path) -> list[str]:
    prefix = _key(directory) + os.sep
    return [Path(k).name for k in _reading if k.startswith(prefix)]


def _renamed(path: str, new: str) -> str:
    """The same path with its folder called ``new``, keeping how it was written."""
    old = Path(path)
    return str(old.parent.with_name(new) / old.name)


def _remember_rename(session: Session, old: str, new: str) -> None:
    table = {key: (new if value == old else value) for key, value in aliases(session).items()}
    table[old] = new
    # ``new`` is a real folder now: nothing is redirected away from it.
    table.pop(new, None)
    table = {key: value for key, value in table.items() if key != value}
    row = session.get(Setting, ALIASES_KEY)
    if row is None:
        session.add(Setting(key=ALIASES_KEY, value=table))
    else:
        row.value = table


def rename_folder(session: Session, old: str, new_name: str,
                  library_root: Path | None = None) -> dict:
    """Rename a library folder on disk and everywhere the database names it.

    ``library_items.folder`` and ``file_path``, and the ``file_path`` of every
    queue entry (published or not) whose file lives in it. The new name is
    made a slug, like every folder the library creates. Videos uploaded later
    whose name says the old series go to the new folder (:func:`destination`).

    Commits: the directory and the rows must change together, so the rename is
    undone on disk when the commit fails. Refused while a file of the folder
    is being uploaded or listened to, and when the new name is taken.
    """
    new = slug(new_name)
    if not new:
        raise ValueError("Escribe un nombre con alguna letra o número.")
    if new == old:
        raise ValueError(f"La carpeta ya se llama «{new}».")
    items = list(session.scalars(select(LibraryItem).where(LibraryItem.folder == old)))
    if not items:
        raise LookupError(f"No hay ninguna carpeta «{old}» en la biblioteca.")

    library_root = Path(library_root or root())
    source, target = library_root / old, library_root / new
    taken = session.scalar(select(func.count(LibraryItem.id)).where(LibraryItem.folder == new))
    if taken or target.exists():
        raise ValueError(f"Ya existe una carpeta «{new}». Elige otro nombre.")
    elsewhere = [i.original_name for i in items
                 if Path(i.file_path).resolve().parent != source.resolve()]
    if elsewhere:
        raise ValueError(
            f"{len(elsewhere)} vídeo(s) de «{old}» no están en media/library/{old}/ "
            f"(p. ej. «{elsewhere[0]}»), así que no se puede renombrar desde aquí.")

    videos = [v for v in session.scalars(
        select(VideoItem).where(VideoItem.file_path.contains(f"{os.sep}{old}{os.sep}",
                                                             autoescape=True)))
        if Path(v.file_path).resolve().parent == source.resolve()]

    with _files_lock:
        busy = _read_under(source)
        busy += [v.original_name for v in videos if v.status == VideoStatus.UPLOADING]
        busy += [x.original_name for x in [*items, *videos] if x.caption_status == "generating"]
        if busy:
            raise ValueError(
                f"Ahora mismo se está subiendo o escuchando «{busy[0]}», de esta carpeta. "
                "Espera a que termine y vuelve a intentarlo.")

        for item in items:
            item.folder = new
            item.file_path = _renamed(item.file_path, new)
        for video in videos:
            video.file_path = _renamed(video.file_path, new)
        _remember_rename(session, old, new)
        labels = folder_labels(session)
        if old in labels:
            labels[new] = labels.pop(old)
            _store_labels(session, labels)
        events.record(session, "library_folder_renamed",
                      f"Biblioteca · carpeta «{old}» renombrada a «{new}» ({len(items)} vídeo(s)"
                      + (f", {len(videos)} en colas" if videos else "") + ")")
        session.flush()

        moved = source.is_dir()
        if moved:
            os.rename(source, target)
        try:
            session.commit()
        except Exception:
            session.rollback()
            if moved:
                os.rename(target, source)
            raise
        if moved:
            _moved[_key(source)] = _key(target)
    return {"old": old, "folder": new, "items": len(items), "videos": len(videos),
            "moved_on_disk": moved}


# --------------------------------------------------------------------------- #
# Moving videos to another folder
# --------------------------------------------------------------------------- #

def move_items(session: Session, items: list[LibraryItem], folder_name: str,
               library_root: Path | None = None) -> dict:
    """Move videos (with their .txt and cover) into another folder, new or not.

    The file goes to ``media/library/<folder>/`` (renamed ``-1``, ``-2``… only
    if the name is taken there), and the library row and every queue entry
    whose file it is (published or not) follow it. A video being uploaded or
    listened to stays where it is and is reported. A folder left empty is
    removed, with its labels. Videos uploaded later still go to the folder
    their series says: a move is for these videos, not for the series.

    Commits, like :func:`rename_folder`: when the commit fails every file goes
    back where it was.
    """
    folder = slug(folder_name)
    if not folder:
        raise ValueError("Escribe un nombre de carpeta con alguna letra o número.")
    library_root = Path(library_root or root())
    target_dir = library_root / folder
    moved: list[tuple[LibraryItem, str, int]] = []
    problems: list[str] = []
    disk: list[tuple[Path, Path]] = []
    left: set[str] = set()

    with _files_lock:
        for item in items:
            if item.folder == folder:
                problems.append(f"«{item.original_name}» ya está en «{folder}».")
                continue
            source = current_path(item.file_path)
            videos = [v for v in {*item.videos, *session.scalars(
                select(VideoItem).where(VideoItem.file_path == item.file_path))}
                if Path(v.file_path).resolve() == source.resolve()]
            if _key(source) in _reading or item.caption_status == "generating" \
                    or any(v.status == VideoStatus.UPLOADING or v.caption_status == "generating"
                           for v in videos):
                problems.append(f"«{item.original_name}» se está subiendo o escuchando ahora; "
                                "repítelo al acabar.")
                continue
            if not source.exists():
                problems.append(f"«{item.original_name}»: el archivo ya no está en la biblioteca.")
                continue
            target = queue.unique_path(target_dir, source.name)
            try:
                disk += _move_with_companions(source, target)
            except OSError as exc:
                problems.append(f"«{item.original_name}»: no se pudo mover ({exc}).")
                continue
            left.add(item.folder)
            item.folder = folder
            item.file_path = str(target)
            for video in videos:
                video.file_path = str(target)
            moved.append((item, str(source), len(videos)))

        if moved:
            labels = folder_labels(session)
            session.flush()
            emptied = [f for f in left if not session.scalar(
                select(func.count(LibraryItem.id)).where(LibraryItem.folder == f))]
            if any(f in labels for f in emptied):
                _store_labels(session, {f: v for f, v in labels.items() if f not in emptied})
            queued = sum(n for _, _, n in moved)
            events.record(session, "library_moved",
                          f"Biblioteca · {len(moved)} vídeo(s) movidos a «{folder}»"
                          + (f" desde {', '.join(f'«{f}»' for f in sorted(left))}" if left else "")
                          + (f" · {queued} en colas" if queued else ""))
            try:
                session.commit()
            except Exception:
                session.rollback()
                _undo_moves(disk)
                raise
            for item, source, _ in moved:
                _moved[_key(source)] = _key(item.file_path)
            for name in emptied:
                directory = library_root / name
                try:
                    if directory.is_dir() and not any(directory.iterdir()):
                        directory.rmdir()
                except OSError:
                    pass
        else:
            # Nothing moved: a target folder made for nothing does not stay.
            try:
                if target_dir.is_dir() and not any(target_dir.iterdir()):
                    target_dir.rmdir()
            except OSError:
                pass
    return {"folder": folder, "moved": len(moved), "problems": problems,
            "videos": sum(n for _, _, n in moved)}
