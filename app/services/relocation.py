"""The app moved to another folder (another Mac, another user): its file paths follow.

The database keeps absolute paths (``library_items.file_path`` and
``video_items.file_path``). After copying ``data/`` and ``media/`` to a new
install, every path under the old project folder is written under this one,
published history included, whether or not the file is still there. Nothing
else changes: no hour, no status, no text. One line in the Activity says it.
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import LibraryItem, VideoItem
from . import events


def moved(path: str | None, old_root: Path, new_root: Path) -> str | None:
    """``path`` under ``new_root`` if it was under ``old_root`` (after collapsing any ``..``)."""
    if not path:
        return None
    normal = Path(os.path.normpath(path))
    old = Path(os.path.normpath(old_root))
    if normal == old or not normal.is_relative_to(old):
        return None
    return str(Path(new_root) / normal.relative_to(old))


def plan(session: Session, old_root: Path, new_root: Path) -> list[tuple[object, str]]:
    """Every row whose path changes, with its new path."""
    out = []
    for model in (LibraryItem, VideoItem):
        for row in session.scalars(select(model)):
            new = moved(row.file_path, old_root, new_root)
            if new is not None and new != row.file_path:
                out.append((row, new))
    return out


def relocate(session: Session, old_root: Path, new_root: Path) -> int:
    """Rewrite the paths and record it. Returns how many rows changed (0: nothing to do)."""
    changes = plan(session, old_root, new_root)
    for row, new in changes:
        row.file_path = new
    if changes:
        events.record(session, "install_relocated",
                      f"Instalación movida: {len(changes)} rutas de vídeo pasan de {old_root} a {new_root}.",
                      data={"from": str(old_root), "to": str(new_root), "rows": len(changes)})
    return len(changes)
