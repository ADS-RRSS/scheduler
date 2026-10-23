"""Moving the media from before the library into it, in two separate steps.

**Copy** (``tools/library_copy.py``). Every video of ``media/inbox``,
``media/posted`` and ``media/failed`` is copied to
``media/library/<series>/`` — once per content, so the reel that was queued
for TikTok and again for YouTube (``…-1.mp4``) becomes one file — together
with its .txt. Each gets its ``library_items`` row, with the texts its queue
rows already have (nothing is transcribed or written again), and those rows
are linked to it. Their ``file_path`` is left alone: the running app keeps
reading exactly what it read before.

On APFS the copy is a clone (``clonefile``): both names share the same
blocks, so copying the whole media folder takes no extra space until one of
the two copies is deleted.

**Finalize** (``tools/library_finalize.py``). Each original is compared byte
for byte with its library copy. Only the verified ones are deleted, and every
row that pointed at them points at the library from then on. A file without
a verified copy is reported and kept.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import LibraryItem, VideoItem, VideoStatus
from . import autocaption, events, library, media, queue

CHUNK = 8 * 1024 * 1024
# A plain copy (where the disk cannot clone) never leaves less than this free.
MIN_FREE_BYTES = 10 * 1024 ** 3
TEXT_SUFFIXES = (".txt", ".caption.txt")
# Rows whose file is being read right now: never repointed under their feet.
IN_FLIGHT = (VideoStatus.UPLOADING, VideoStatus.PROCESSING)


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #

def _libc():
    if sys.platform != "darwin":
        return None
    try:
        return ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
    except OSError:
        return None


_LIBC = _libc()


class NoSpace(OSError):
    """A plain copy would leave the disk with less than MIN_FREE_BYTES free."""


def clone_or_copy(src: Path, dst: Path) -> str:
    """Copy ``src`` to ``dst`` (which must not exist). Returns "clone" or "copy".

    A clone is an APFS copy-on-write copy: instant, and it takes no space
    until one side changes. Where the filesystem cannot clone, it is a plain
    copy, which needs the space.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(f".{dst.name}.part")
    part.unlink(missing_ok=True)
    how = "copy"
    if _LIBC is not None and hasattr(_LIBC, "clonefile"):
        if _LIBC.clonefile(os.fsencode(src), os.fsencode(part), ctypes.c_int(0)) == 0:
            how = "clone"
    if how == "copy":
        free = shutil.disk_usage(dst.parent).free
        size = src.stat().st_size
        if free < size + MIN_FREE_BYTES:
            raise NoSpace(f"este disco no permite clonar y copiar «{src.name}» "
                          f"({size >> 20} MB) dejaría menos de "
                          f"{MIN_FREE_BYTES >> 30} GB libres ({free >> 20} MB ahora)")
        shutil.copy2(src, part)
    # Renamed only once complete: an interrupted run never leaves a file that
    # looks copied.
    os.replace(part, dst)
    return how


def same_bytes(a: Path, b: Path) -> bool:
    """Byte-for-byte comparison of two files."""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        with a.open("rb") as fa, b.open("rb") as fb:
            while True:
                chunk = fa.read(CHUNK)
                if chunk != fb.read(CHUNK):
                    return False
                if not chunk:
                    return True
    except OSError:
        return False


def legacy_videos(settings: Settings) -> list[Path]:
    """The videos still in the old media folders."""
    found = []
    for directory in settings.legacy_media_dirs:
        if directory.exists():
            found += [p for p in directory.iterdir()
                      if p.is_file() and p.suffix.lower() in queue.VIDEO_SUFFIXES]
    return sorted(found)


def legacy_texts(settings: Settings) -> list[Path]:
    found = []
    for directory in settings.legacy_media_dirs:
        if directory.exists():
            found += [p for p in directory.iterdir()
                      if p.is_file() and p.suffix.lower() == ".txt"]
    return sorted(found)


def sidecars(video: Path) -> list[Path]:
    return [video.with_suffix(s) for s in TEXT_SUFFIXES if video.with_suffix(s).exists()]


@contextmanager
def scratch_session(settings: Settings) -> Iterator[Session]:
    """A session on a throwaway copy of the database, for the dry runs.

    A dry run must not change the real file at all, not even its schema, so
    it works on a copy that is deleted afterwards.
    """
    from sqlalchemy import create_engine

    from ..db import init_additive

    source = settings.database_path
    with tempfile.TemporaryDirectory(prefix="tts-dry-") as tmp:
        target = Path(tmp) / "dry-run.db"
        if source is not None and source.exists():
            src, dst = sqlite3.connect(str(source)), sqlite3.connect(str(target))
            try:
                with dst:
                    src.backup(dst)
            finally:
                dst.close()
                src.close()
        scratch = create_engine(f"sqlite:///{target}", future=True)
        try:
            init_additive(scratch)
            session = Session(scratch, autoflush=True, expire_on_commit=False)
            try:
                yield session
            finally:
                session.close()
        finally:
            scratch.dispose()


def backup_database(settings: Settings) -> Path | None:
    """A consistent copy of the database, named like the ones Ajustes lists."""
    source = settings.database_path
    if source is None or not source.exists():
        return None
    directory = Path(settings.backups_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / datetime.now().strftime("scheduler-%Y%m%d-%H%M%S.db")
    src, dst = sqlite3.connect(str(source)), sqlite3.connect(str(target))
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


# --------------------------------------------------------------------------- #
# Step 1: copy
# --------------------------------------------------------------------------- #

@dataclass
class CopyReport:
    groups: int = 0
    copied: int = 0
    cloned: int = 0
    already: int = 0
    registered: int = 0
    linked_rows: int = 0
    texts_copied: int = 0
    to_write: int = 0
    bytes_copied: int = 0
    bytes_saved: int = 0
    folders: set[str] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)


def _canonical(paths: list[Path]) -> Path:
    """The name the library keeps for a group of identical files.

    The shortest wins: when the same reel was queued twice, the second copy
    got a "-1" the first one does not have.
    """
    return min(paths, key=lambda p: (len(p.name), p.name))


def _platform_rows(rows: list[VideoItem]) -> dict[str, list[VideoItem]]:
    out: dict[str, list[VideoItem]] = defaultdict(list)
    for row in sorted(rows, key=lambda r: (r.caption_status != "done", -r.id)):
        out[library.platform_of(row.account)].append(row)
    return out


def texts_from_rows(rows: list[VideoItem], sheet) -> dict:
    """The library texts of a video that already has queue rows.

    Each platform takes its own row's texts; a field that row lacks (TikTok
    rows written before YouTube existed have no title and no pinned comment)
    comes from the other platform's row of the same video, and failing that
    from the reel sheet, exactly as a video entering now would get them.
    """
    by_platform = _platform_rows(rows)
    everyone = [r for p in library.PLATFORMS for r in by_platform.get(p, [])]
    fallback = autocaption.sheet_fallback(sheet) if sheet is not None else None
    name = rows[0].original_name if rows else ""

    def pick(attr: str, first: list[VideoItem]):
        for row in first + [r for r in everyone if r not in first]:
            if getattr(row, attr):
                return getattr(row, attr)
        return None

    texts = {}
    for platform in library.PLATFORMS:
        mine = by_platform.get(platform, [])
        texts[platform] = {
            "title": pick("title", mine) or (fallback.title if fallback else "")
            or autocaption.topic_from_filename(name),
            "description": pick("caption", mine) or "",
            "tags": list(pick("auto_hashtags", mine) or []),
            "first_comment": pick("first_comment", mine)
            or (fallback.first_comment if fallback else ""),
        }

    best = everyone[0] if everyone else None
    if best is None:
        status = "none"
    elif best.caption_status == "failed" and not best.caption:
        status = "failed"
    else:
        # Written (by hand or by the model) or nothing to write: either way
        # nothing is pending, the queue rows already went with these texts.
        status = "done" if best.caption_source not in (None, "manual") else "none"
    return {
        "texts": texts,
        "status": status,
        "source": best.caption_source if best is not None else None,
        "error": best.caption_error if best is not None and status == "failed" else None,
        "transcript": next((r.transcript for r in everyone if r.transcript), None),
        "generated_at": next((r.caption_generated_at for r in everyone
                              if r.caption_generated_at), None),
    }


def _twin(video: Path, text: Path) -> Path:
    """Where the .txt of ``text``'s kind sits next to ``video``."""
    return video.with_suffix(".caption.txt" if text.name.endswith(".caption.txt") else ".txt")


def _register_copy(session: Session, target: Path, digest: str,
                   rows: list[VideoItem]) -> LibraryItem:
    """The library row of a copied file, with its queue rows' texts."""
    if not rows:
        # A file nobody had queued: it enters like any new video, and its
        # texts are written by the captioner if they are on.
        result = library.register(session, target, folder=target.parent.name,
                                  checksum=digest, record_event=False)
        return result.item or result.duplicate_of
    check = media.preflight(target)
    found = texts_from_rows(rows, queue.read_sheet(target))
    item = LibraryItem(
        folder=target.parent.name, file_path=str(target), original_name=target.name,
        checksum=digest, size_bytes=target.stat().st_size, probe=check.probe.as_dict(),
        preflight_errors=check.errors or None, preflight_warnings=check.warnings or None,
        caption_status=found["status"], caption_source=found["source"],
        caption_error=found["error"], transcript=found["transcript"],
        caption_generated_at=found["generated_at"],
    )
    for platform, texts in found["texts"].items():
        library.set_texts(item, platform, **texts)
    session.add(item)
    session.flush()
    return item


def copy_all(session: Session, settings: Settings, *, dry_run: bool = True,
             progress=None) -> CopyReport:
    """Step 1: copy every old video (and .txt) into the library and register it.

    Commits after each video (not in a dry run), so it can run next to the
    app without holding SQLite's write lock for minutes, and an interruption
    loses at most the video in hand. Running it again resumes.
    """
    report = CopyReport()
    lib_root = library.root(settings)
    rows_by_sum: dict[str, list[VideoItem]] = defaultdict(list)
    for row in session.scalars(select(VideoItem)):
        rows_by_sum[row.checksum].append(row)

    groups: dict[str, list[Path]] = defaultdict(list)
    videos = legacy_videos(settings)
    for i, path in enumerate(videos, 1):
        try:
            groups[media.checksum(path)].append(path)
        except OSError as exc:
            report.problems.append(f"«{path.name}»: no se pudo leer ({exc})")
        if progress:
            progress("leyendo", i, len(videos))
    report.groups = len(groups)

    for n, (digest, paths) in enumerate(sorted(groups.items(), key=lambda g: _canonical(g[1]).name), 1):
        if progress:
            progress("copiando", n, len(groups))
        source = _canonical(paths)
        size = source.stat().st_size
        report.bytes_saved += size * (len(paths) - 1)
        existing = library.by_checksum(session, digest)
        if existing is not None and Path(existing.file_path).exists():
            target = Path(existing.file_path)
            report.already += 1
        else:
            folder = library.destination(session, source.name)
            report.folders.add(folder)
            target = lib_root / folder / source.name
            if target.exists() and not same_bytes(source, target):
                target = queue.unique_path(target.parent, source.name)
            if dry_run:
                report.planned.append(f"{source} -> {target}")
                report.copied += 1
                report.bytes_copied += size
                if not rows_by_sum.get(digest):
                    report.to_write += 1
                continue
            if not target.exists():
                try:
                    how = clone_or_copy(source, target)
                except NoSpace as exc:
                    # Every file after this one would hit the same wall.
                    report.problems.append(f"Copia detenida: {exc}")
                    break
                except OSError as exc:
                    report.problems.append(f"«{source.name}»: {exc}")
                    continue
                report.copied += 1
                report.bytes_copied += size
                if how == "clone":
                    report.cloned += 1
            else:
                report.already += 1

        if dry_run:
            continue
        # The .txt goes before the row: the row reads it (a sheet's title).
        texts = next((sidecars(p) for p in [source, *paths] if sidecars(p)), [])
        for text in texts:
            wanted = _twin(target, text)
            if not wanted.exists():
                shutil.copy2(text, wanted)
                report.texts_copied += 1
        item = existing if existing is not None else None
        if item is None:
            item = _register_copy(session, target, digest, rows_by_sum.get(digest, []))
            report.registered += 1
            if item.caption_status == "pending":
                report.to_write += 1
        elif Path(item.file_path) != target:
            item.file_path = str(target)
        for row in rows_by_sum.get(digest, []):
            if row.library_item_id != item.id:
                row.library_item_id = item.id
                report.linked_rows += 1
        # One short transaction per video: the app may be running (and
        # uploading) meanwhile, and SQLite lets one writer through at a time.
        # Everything slow (the clone, ffprobe) happened before this point.
        session.commit()

    # Sheets whose video is gone still go to their folder: the video may come back.
    for text in legacy_texts(settings):
        if any(text.with_suffix(s).exists() for s in queue.VIDEO_SUFFIXES):
            continue
        if text.name.endswith(".caption.txt") and any(
                Path(str(text)[:-len(".caption.txt")] + s).exists() for s in queue.VIDEO_SUFFIXES):
            continue
        target = lib_root / library.destination(session, text.name) / text.name
        if target.exists():
            continue
        if dry_run:
            report.planned.append(f"{text} -> {target}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(text, target)
        report.texts_copied += 1

    if not dry_run and (report.registered or report.linked_rows):
        events.record(
            session, "library_imported",
            f"Biblioteca · {report.registered} vídeo(s) copiados desde media/ en "
            f"{len(report.folders)} carpeta(s); {report.linked_rows} vídeo(s) de las colas "
            "enlazados a ellos. Los originales siguen en su sitio hasta "
            "tools/library_finalize.py.",
            level="success",
        )
        session.commit()
    return report


# --------------------------------------------------------------------------- #
# Step 2: verify, delete the originals, point the rows at the library
# --------------------------------------------------------------------------- #

@dataclass
class FinalizeReport:
    verified: int = 0
    deleted: int = 0
    texts_deleted: int = 0
    rows_repointed: int = 0
    bytes_freed: int = 0
    kept: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _rows_at(session: Session, path: Path) -> list[VideoItem]:
    candidates = {str(path), str(path.resolve())}
    return list(session.scalars(select(VideoItem).where(VideoItem.file_path.in_(candidates))))


def _repoint(rows: list[VideoItem], item: LibraryItem) -> int:
    for row in rows:
        row.file_path = item.file_path
        row.library_item_id = item.id
    return len(rows)


def finalize(session: Session, settings: Settings, *, apply: bool = False,
             progress=None) -> FinalizeReport:
    """Step 2. Only what is verified is deleted; everything else is kept and listed.

    A video counts as copied when its library copy has the same bytes and,
    if it came with a .txt, the library has one too. Its rows are repointed
    and committed before the original is deleted, so an interruption never
    leaves a row pointing at a file that is gone.
    """
    report = FinalizeReport()
    videos = legacy_videos(settings)
    for i, path in enumerate(videos, 1):
        if progress:
            progress("verificando", i, len(videos))
        try:
            item = library.by_checksum(session, media.checksum(path))
        except OSError as exc:
            report.kept.append(f"{path}: no se pudo leer ({exc})")
            continue
        if item is None:
            report.kept.append(f"{path}: no está en la biblioteca (ejecuta tools/library_copy.py)")
            continue
        copy = Path(item.file_path)
        if not library.owns(copy, settings) or not copy.exists():
            report.kept.append(f"{path}: su copia de la biblioteca no existe ({copy})")
            continue
        if not same_bytes(path, copy):
            report.kept.append(f"{path}: la copia de la biblioteca no es idéntica ({copy})")
            continue
        texts = sidecars(path)
        missing = [t for t in texts if not _twin(copy, t).exists()]
        if missing:
            report.kept.append(f"{path}: falta su .txt en la biblioteca (ejecuta "
                               "tools/library_copy.py otra vez)")
            continue
        rows = _rows_at(session, path)
        if any(r.status in IN_FLIGHT for r in rows):
            report.kept.append(f"{path}: se está subiendo ahora mismo; vuelve a intentarlo luego")
            continue
        report.verified += 1
        if not apply:
            report.rows_repointed += len(rows)
            report.deleted += 1
            report.bytes_freed += path.stat().st_size
            continue
        report.rows_repointed += _repoint(rows, item)
        session.commit()
        size = path.stat().st_size
        path.unlink()
        report.deleted += 1
        report.bytes_freed += size
        for text in texts:
            twin = _twin(copy, text)
            if same_bytes(text, twin):
                text.unlink()
                report.texts_deleted += 1
            else:
                report.notes.append(f"{text}: se conserva, su .txt de la biblioteca es distinto "
                                    f"({twin})")

    # Sheets without a video, copied on their own.
    for text in legacy_texts(settings):
        if not text.exists() or any(text.with_suffix(s).exists() for s in queue.VIDEO_SUFFIXES):
            continue
        twin = library.root(settings) / library.destination(session, text.name) / text.name
        if not same_bytes(text, twin):
            report.kept.append(f"{text}: no hay copia idéntica en la biblioteca")
            continue
        if apply:
            text.unlink()
        report.texts_deleted += 1

    # Rows whose old file is already gone, but whose video is in the library.
    for row in session.scalars(select(VideoItem)):
        if library.owns(row.file_path, settings) or Path(row.file_path).exists():
            continue
        if row.status in IN_FLIGHT:
            continue
        item = library.by_checksum(session, row.checksum)
        if item is None or not Path(item.file_path).exists():
            continue
        report.rows_repointed += 1
        if apply:
            _repoint([row], item)
    if apply:
        if report.deleted or report.rows_repointed:
            events.record(
                session, "library_finalized",
                f"Biblioteca · {report.deleted} original(es) de media/ verificados y borrados "
                f"({report.bytes_freed / 1024 ** 3:.1f} GB); {report.rows_repointed} vídeo(s) "
                "de las colas apuntan ya a la biblioteca.",
                level="success",
            )
        session.commit()
    return report


def app_is_running(settings: Settings) -> bool:
    """Is something answering on the app's port?"""
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((settings.host, settings.port)) == 0
