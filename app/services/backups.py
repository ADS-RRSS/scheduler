"""Copies of the database: by hand, before a wipe, and on their own once a day.

The database is the only record of what was handed to Zernio: hundreds of
posts scheduled there with nothing else on this machine saying which video
each one is. Losing it means losing the way to take them back, so it is
copied every ``BACKUP_AUTO_HOURS`` without anyone asking, and the automatic
copies are rotated (the last ``BACKUP_KEEP`` stay). The ones made by hand are
never deleted here.

Every copy goes through SQLite's online backup API: consistent while the app
keeps writing, WAL included, which a plain file copy of ``scheduler.db`` is not.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings, get_settings

AUTO_PREFIX = "scheduler-auto-"
NAME_RE = re.compile(r"^scheduler-(?:auto-)?\d{8}-\d{6}\.db$")


class BackupError(RuntimeError):
    pass


def directory(settings: Settings | None = None) -> Path:
    return Path((settings or get_settings()).backups_dir)


def copy_database(source: Path, target: Path) -> int:
    """A consistent copy of a live SQLite file. Returns its size."""
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(target))
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target.stat().st_size


def create(settings: Settings | None = None, *, auto: bool = False) -> tuple[str, int]:
    """Copy the database into the backups folder. Returns ``(name, bytes)``."""
    settings = settings or get_settings()
    source = settings.database_path
    if source is None or not source.exists():
        raise BackupError("La base de datos no es un archivo SQLite local.")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{AUTO_PREFIX if auto else 'scheduler-'}{stamp}.db"
    target = directory(settings) / name
    return name, copy_database(source, target)


def list_all(folder: Path | None = None) -> list[dict]:
    """Every copy, newest first, saying which ones were automatic."""
    folder = folder or directory()
    if not folder.exists():
        return []
    rows = []
    for entry in folder.glob("scheduler-*.db"):
        if not NAME_RE.match(entry.name):
            continue
        stat = entry.stat()
        rows.append({
            "name": entry.name,
            "bytes": stat.st_size,
            "auto": entry.name.startswith(AUTO_PREFIX),
            "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        })
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return rows


def latest_auto(folder: Path | None = None) -> datetime | None:
    """When the newest automatic copy was made (its file's time survives restarts)."""
    autos = [r for r in list_all(folder) if r["auto"]]
    return autos[0]["created_at"] if autos else None


def prune_auto(keep: int, folder: Path | None = None) -> int:
    """Delete the automatic copies beyond the newest ``keep``. Returns how many."""
    autos = [r for r in list_all(folder) if r["auto"]]
    removed = 0
    for row in autos[max(keep, 1):]:
        (folder or directory()).joinpath(row["name"]).unlink(missing_ok=True)
        removed += 1
    return removed


def due(settings: Settings | None = None, now: datetime | None = None) -> bool:
    """Is an automatic copy due? Never when ``BACKUP_AUTO_HOURS`` is 0."""
    settings = settings or get_settings()
    if settings.backup_auto_hours <= 0 or settings.database_path is None:
        return False
    last = latest_auto(directory(settings))
    now = now or datetime.now(timezone.utc)
    return last is None or (now - last).total_seconds() >= settings.backup_auto_hours * 3600


def run_auto(settings: Settings | None = None) -> tuple[str, int, int] | None:
    """Make the daily copy if it is due, and rotate. ``(name, bytes, pruned)`` or None."""
    settings = settings or get_settings()
    if not due(settings):
        return None
    name, size = create(settings, auto=True)
    pruned = prune_auto(settings.backup_keep, directory(settings))
    return name, size, pruned
