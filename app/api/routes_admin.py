"""Admin endpoints: system info, backups, maintenance and the wipe button."""
from __future__ import annotations

import logging
import platform
import secrets
import shutil
import sqlite3
import subprocess
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import APP_VERSION, get_settings
from ..db import SCHEMA_VERSION, engine, get_session, init_db
from ..models import Account, Base, VideoItem, VideoStatus
from ..scheduler import worker as worker_mod
from ..schemas import ClearFailedIn, PruneEventsIn, WipeAnswerIn, WipeIn
from ..services import backups as backups_svc
from ..services import events, media
from ..services import library as library_svc
from ..services import queue as queue_svc
from .serializers import iso
from .routes_api import ai_status, disk_status

log = logging.getLogger("scheduler")

router = APIRouter(prefix="/api/admin", tags=["admin"])

def _dir_stats(path: Path, recursive: bool = False) -> dict:
    count, size = 0, 0
    if path.exists():
        for entry in (path.rglob("*") if recursive else path.iterdir()):
            if any(part.startswith(".") for part in entry.relative_to(path).parts):
                continue
            if entry.is_file():
                count += 1
                size += entry.stat().st_size
    return {"path": str(path), "count": count, "bytes": size}


def _db_size(path: Path | None) -> int:
    if path is None:
        return 0
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            total += candidate.stat().st_size
    return total


def _ffmpeg_version() -> str | None:
    if not media.ffprobe_available():
        return None
    try:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        return out.stdout.splitlines()[0][:80] if out.stdout else "disponible"
    except (OSError, subprocess.TimeoutExpired):
        return "disponible"


def _list_backups(directory: Path) -> list[dict]:
    return [{**row, "created_at": iso(row["created_at"])} for row in backups_svc.list_all(directory)]


@router.get("/system")
def system_info(session: Session = Depends(get_session)) -> dict:
    settings = get_settings()
    worker = worker_mod.worker
    uptime = (
        (datetime.now(timezone.utc) - worker.started_at).total_seconds()
        if worker.started_at else None
    )
    return {
        "version": APP_VERSION,
        "schema_version": SCHEMA_VERSION,
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "ffmpeg": _ffmpeg_version(),
        "database": {"path": str(settings.database_path) if settings.database_path else
                     settings.database_url, "bytes": _db_size(settings.database_path)},
        "media": {
            "library": _dir_stats(Path(settings.media_library_dir), recursive=True),
            "inbox": _dir_stats(Path(settings.media_inbox_dir)),
            "posted": _dir_stats(Path(settings.media_posted_dir)),
            "failed": _dir_stats(Path(settings.media_failed_dir)),
        },
        "scheduler": {
            "running": worker.running,
            "started_at": iso(worker.started_at),
            "uptime_seconds": uptime,
            "tick_seconds": settings.scheduler_tick_seconds,
            "tick_count": worker.tick_count,
            "last_tick_at": iso(worker.last_tick_at),
            "last_error": worker.last_error,
            "paused": worker_mod.is_paused(session),
        },
        "zernio_api_base": settings.zernio_api_base,
        "passcode_enabled": bool(settings.ui_passcode),
        "ai": ai_status(),
        "disk": disk_status(settings),
        "backups": {"dir": str(settings.backups_dir), "items": _list_backups(Path(settings.backups_dir)),
                    "auto_hours": settings.backup_auto_hours, "keep": settings.backup_keep},
        "accounts": session.query(Account).count(),
        "videos": {
            status.value: session.query(VideoItem).filter(VideoItem.status == status).count()
            for status in VideoStatus
        },
    }


# --------------------------------------------------------------------------- #
# Backups
# --------------------------------------------------------------------------- #

@router.get("/backups")
def list_backups() -> dict:
    return {"backups": _list_backups(Path(get_settings().backups_dir))}


def _backup_now() -> tuple[str, int]:
    """Consistent copy of the live database via SQLite's online backup API."""
    try:
        return backups_svc.create(get_settings())
    except backups_svc.BackupError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/backups")
def create_backup(session: Session = Depends(get_session)) -> dict:
    name, size = _backup_now()
    events.record(session, "backup_created", f"Copia de la base de datos creada: {name}")
    session.commit()
    return {"name": name, "bytes": size}


def _backup_or_404(name: str) -> Path:
    if not backups_svc.NAME_RE.match(name):
        raise HTTPException(400, "Nombre de copia inválido.")
    path = Path(get_settings().backups_dir) / name
    if not path.exists():
        raise HTTPException(404, "Copia no encontrada.")
    return path


@router.get("/backups/{name}")
def download_backup(name: str):
    path = _backup_or_404(name)
    return FileResponse(str(path), media_type="application/vnd.sqlite3", filename=name)


@router.delete("/backups/{name}")
def delete_backup(name: str) -> dict:
    _backup_or_404(name).unlink()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Maintenance
# --------------------------------------------------------------------------- #

@router.post("/events/prune")
def prune_events(body: PruneEventsIn, session: Session = Depends(get_session)) -> dict:
    removed = events.prune_to(session, body.keep)
    events.record(session, "events_pruned", f"Registro de actividad recortado: {removed} entradas.")
    session.commit()
    return {"removed": removed}


@router.post("/videos/clear-failed")
def clear_failed(body: ClearFailedIn, session: Session = Depends(get_session)) -> dict:
    query = session.query(VideoItem).filter(VideoItem.status == VideoStatus.FAILED)
    if body.account_id is not None:
        query = query.filter(VideoItem.account_id == body.account_id)
    items = query.all()
    for item in items:
        # A library file is shared with the other queues: it stays, and is
        # deleted from the library page if it has to go.
        if body.delete_files and not library_svc.owns(item.file_path):
            Path(item.file_path).unlink(missing_ok=True)
        session.delete(item)
    events.record(session, "failed_cleared",
                  f"{len(items)} vídeo(s) fallido(s) eliminado(s) de la cola"
                  + (" y del disco" if body.delete_files else "") + ".",
                  level="warn", account_id=body.account_id)
    session.commit()
    return {"removed": len(items)}


# --------------------------------------------------------------------------- #
# Wipe: empty the database and free the disk
# --------------------------------------------------------------------------- #
#
# The one button in this tool that cannot be undone from inside it, so it is
# gated by three consecutive challenges: an addition and a word to copy, both
# checked on the server. Getting one wrong throws the whole thing away and the
# three start again — a stray double click cannot walk through them, and no
# saved answer can either, because each challenge is generated on the spot.

WIPE_STEPS = 3
WIPE_TTL_MINUTES = 5
# Short, unambiguous Spanish words: they are typed by hand under a red banner.
WIPE_WORDS = (
    "naranja", "ventana", "camino", "botella", "montaña", "silla", "cuchara",
    "jardin", "ladrillo", "manzana", "tornillo", "bicicleta", "farola", "pimienta",
)
# token -> {"step", "total", "word", "expires"}
_wipe_challenges: dict[str, dict] = {}


def _fold(value: str) -> str:
    """Casefolded and accent-free, so «montaña» also matches "montana"."""
    stripped = unicodedata.normalize("NFKD", (value or "").strip())
    return "".join(c for c in stripped if not unicodedata.combining(c)).casefold()


def _new_challenge(token: str, step: int) -> dict:
    left, right = secrets.randbelow(8) + 2, secrets.randbelow(8) + 2
    word = secrets.choice(WIPE_WORDS)
    _wipe_challenges[token] = {
        "step": step,
        "total": left + right,
        "word": word,
        "expires": datetime.now(timezone.utc) + timedelta(minutes=WIPE_TTL_MINUTES),
    }
    return {"token": token, "step": step, "steps": WIPE_STEPS,
            "question": f"{left} + {right}", "word": word}


def _live_challenge(token: str) -> dict:
    for expired in [t for t, c in _wipe_challenges.items()
                    if c["expires"] < datetime.now(timezone.utc)]:
        _wipe_challenges.pop(expired, None)
    challenge = _wipe_challenges.get(token)
    if challenge is None:
        raise HTTPException(409, "La confirmación ha caducado o se ha cancelado. "
                                 "Empieza de nuevo.")
    return challenge


def _video_files(directory: Path, recursive: bool = False) -> list[Path]:
    """Every video this app manages inside one of its own folders.

    Only the library and the three old media folders are ever touched:
    whatever else lives in the project stays where it is, even if it is an
    .mp4. The library is walked into its series folders; the others never
    had subfolders.
    """
    if not directory.exists():
        return []
    entries = directory.rglob("*") if recursive else directory.iterdir()
    return sorted(
        entry for entry in entries
        if entry.is_file() and entry.suffix.lower() in queue_svc.VIDEO_SUFFIXES
    )


def _media_videos(settings) -> list[Path]:
    """The videos a wipe deletes: the library's and the old folders'."""
    found = _video_files(Path(settings.media_library_dir), recursive=True)
    for directory in settings.legacy_media_dirs:
        found += _video_files(directory)
    return found


def _wipe_preview(session: Session) -> dict:
    """What pressing the button would destroy, counted before asking."""
    settings = get_settings()
    tables = {}
    for table in Base.metadata.sorted_tables:
        tables[table.name] = session.scalar(
            select(func.count()).select_from(table)
        ) or 0
    files, size = 0, 0
    for path in _media_videos(settings):
        files += 1
        size += path.stat().st_size
    return {
        "tables": tables,
        "rows": sum(tables.values()),
        "files": files,
        "bytes": size,
        "database_bytes": _db_size(settings.database_path),
        "folders": [str(d) for d in settings.media_dirs],
        # A post already parked in Zernio is not ours to forget: deleting the
        # row here does not stop TikTok publishing it.
        "delegated": session.scalar(
            select(func.count()).select_from(VideoItem).where(
                VideoItem.status == VideoStatus.DELEGATED
            )
        ) or 0,
        "in_flight": len(queue_svc.in_flight(session)),
    }


@router.get("/wipe/preview")
def wipe_preview(session: Session = Depends(get_session)) -> dict:
    return _wipe_preview(session)


@router.post("/wipe/challenge")
def wipe_challenge(session: Session = Depends(get_session)) -> dict:
    """Start the three confirmations. Any previous attempt is dropped."""
    _wipe_challenges.clear()
    token = secrets.token_urlsafe(16)
    return {**_new_challenge(token, 1), "preview": _wipe_preview(session)}


@router.post("/wipe/answer")
def wipe_answer(body: WipeAnswerIn) -> dict:
    """Check one confirmation and hand out the next, or the go-ahead."""
    challenge = _live_challenge(body.token)
    if body.total != challenge["total"] or _fold(body.word) != _fold(challenge["word"]):
        _wipe_challenges.pop(body.token, None)
        raise HTTPException(
            400, f"La comprobación {challenge['step']} de {WIPE_STEPS} no es correcta. "
                 "Se cancela el borrado: vuelve a empezar si de verdad quieres hacerlo."
        )
    if challenge["step"] >= WIPE_STEPS:
        challenge["step"] = WIPE_STEPS + 1  # ready, and only for this token
        return {"token": body.token, "step": challenge["step"], "steps": WIPE_STEPS,
                "ready": True}
    return {**_new_challenge(body.token, challenge["step"] + 1), "ready": False}


@router.post("/wipe")
def wipe(body: WipeIn, session: Session = Depends(get_session)) -> dict:
    """Empty every table and delete every video file of the media folders.

    A backup of the database is written first: it is the only way back, and it
    stays in data/backups (a few MB), so the disk space the videos were using
    is still freed.
    """
    challenge = _live_challenge(body.token)
    if challenge["step"] <= WIPE_STEPS:
        raise HTTPException(409, "Faltan confirmaciones por completar.")
    if queue_svc.in_flight(session):
        raise HTTPException(
            409, "Hay un vídeo subiéndose ahora mismo. Espera a que termine y vuelve "
                 "a intentarlo."
        )
    _wipe_challenges.pop(body.token, None)

    settings = get_settings()
    before = _wipe_preview(session)
    backup_name, backup_bytes = _backup_now()

    # Children first: the foreign keys are ON, and a wipe is not the place to
    # find out which cascade fires.
    for table in reversed(Base.metadata.sorted_tables):
        session.execute(table.delete())
    session.commit()
    session.close()

    # Rebuild the empty schema (and re-stamp its version: the settings row went
    # with the rest), then hand the pages back to the filesystem.
    init_db()
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("VACUUM")
    except Exception:  # noqa: BLE001 - compacting is a bonus, not the job
        log.warning("VACUUM after the wipe failed; the database is empty anyway")

    deleted, freed = 0, 0
    for path in _media_videos(settings):
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            continue
        deleted += 1
        freed += size
        # The caption that travelled with the video has nothing left to say.
        for suffix in (".txt", ".caption.txt"):
            path.with_suffix(suffix).unlink(missing_ok=True)
    # The library's series folders that are now empty go too.
    library_root = Path(settings.media_library_dir)
    if library_root.exists():
        for folder in sorted(library_root.iterdir(), reverse=True):
            try:
                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
            except OSError:
                continue

    events.record(
        session, "database_wiped",
        f"Base de datos vaciada: {before['rows']} fila(s) y {deleted} vídeo(s) borrados "
        f"({freed / (1024 * 1024):.0f} MB liberados). Copia previa: {backup_name}.",
        level="warn",
    )
    session.commit()
    return {
        "rows": before["rows"],
        "tables": before["tables"],
        "files_deleted": deleted,
        "bytes_freed": freed,
        "delegated_left_in_zernio": before["delegated"],
        "backup": {"name": backup_name, "bytes": backup_bytes},
        "message": (
            f"Borrado completo: {before['rows']} fila(s) y {deleted} vídeo(s) "
            f"({freed / (1024 * 1024):.0f} MB). Copia de seguridad: {backup_name}."
        ),
    }


@router.get("/disk")
def disk() -> dict:
    usage = shutil.disk_usage(str(get_settings().media_library_dir))
    return {"total": usage.total, "used": usage.used, "free": usage.free,
            "python_executable": sys.executable}
