"""Videos whose file is gone: what the queues and the library can let go of.

After the database travels to another Mac without ``media/`` (Tunnel's
migration package without «medios»), every queued video and every library
entry points at a file that is not there. Nothing of that can ever be
published from here, and the worker would mark each one failed as it reached
it. This module finds those rows and removes what is only ours:

* a **queued** or **scheduled** video, or one marked for Zernio that Zernio has
  not received yet (``DELEGATED`` without ``zernio_post_id``): it goes, and its
  queue is replanned;
* a video **in Zernio's hands** (uploading, processing, parked in Zernio with
  its post id) keeps its row: Zernio has the bytes and publishes it on its own;
* **published** and **failed** videos are history and stay;
* a **library entry** without its file goes once no queue still needs it here;
  the videos it had just stop pointing at it, like a normal library delete.

What was made on this Mac has its file on disk, so it never matches: the one
rule is «the file is not where the row says», not a date.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import EventLog, LibraryItem, Schedule, VideoItem, VideoStatus
from . import events, queue, scheduling
from . import library as library_svc

# Still ours: nothing has reached Zernio, so dropping the row drops the video.
LOCAL_STATUSES = (VideoStatus.QUEUED, VideoStatus.SCHEDULED)
# Zernio already has the bytes (or is getting them): the row stays, file or no file.
ZERNIO_STATUSES = (VideoStatus.UPLOADING, VideoStatus.PROCESSING)


def exists(path: str | None) -> bool:
    """Is the file where the row says (or where the library moved it)?"""
    if not path:
        return False
    return library_svc.current_path(path).exists()


def is_local(video: VideoItem) -> bool:
    """Not yet in Zernio's hands: a delete here is a real delete."""
    return video.status in LOCAL_STATUSES or (
        video.status == VideoStatus.DELEGATED and not video.zernio_post_id)


@dataclass
class Report:
    """What :func:`find` saw. Counts first; the rows only matter to :func:`clean`."""

    local: list[VideoItem] = field(default_factory=list)      # to delete
    zernio: list[VideoItem] = field(default_factory=list)     # kept: Zernio has them
    history: list[VideoItem] = field(default_factory=list)    # kept: published or failed
    library: list[LibraryItem] = field(default_factory=list)  # to delete
    intact_videos: int = 0
    intact_library: int = 0

    def queues(self) -> list[dict]:
        """How many videos leave each queue: «@cuenta · Cola» → n."""
        per: dict[int | None, dict] = {}
        for v in self.local:
            s = v.schedule
            key = s.id if s is not None else None
            if key not in per:
                account = v.account
                who = (account.display_name or f"@{account.username}") if account is not None else ""
                per[key] = {"schedule_id": key, "account": who,
                            "name": s.name if s is not None else "sin cola", "count": 0}
            per[key]["count"] += 1
        return sorted(per.values(), key=lambda q: (-q["count"], q["account"], q["name"]))

    def folders(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.library:
            out[item.folder] = out.get(item.folder, 0) + 1
        return dict(sorted(out.items()))

    def json(self) -> dict:
        return {
            "videos": {"local": len(self.local), "zernio": len(self.zernio), "history": len(self.history)},
            "library": len(self.library),
            "queues": self.queues(),
            "folders": self.folders(),
            "intact": {"videos": self.intact_videos, "library": self.intact_library},
            "total": len(self.local) + len(self.library),
        }


def find(session: Session) -> Report:
    report = Report()
    for video in session.scalars(select(VideoItem)):
        if exists(video.file_path):
            report.intact_videos += 1
        elif is_local(video):
            report.local.append(video)
        elif video.status in ZERNIO_STATUSES or video.status == VideoStatus.DELEGATED:
            report.zernio.append(video)
        else:
            report.history.append(video)
    gone = {v.id for v in report.local}
    for item in session.scalars(select(LibraryItem)):
        if exists(item.file_path):
            report.intact_library += 1
        # A library entry leaves only when every queue that still needs it here is leaving too; one
        # parked in Zernio keeps its entry (recovering the post would bring it back to the library).
        elif all(v.id in gone or not library_svc.needs_file(v) for v in item.videos):
            report.library.append(item)
    return report


def relocated(session: Session) -> dict | None:
    """The last time this install was moved from another folder (Tunnel's migration), if the Activity
    still has it: a hint for the user about where the missing files come from."""
    row = session.scalars(select(EventLog).where(EventLog.kind == "install_relocated")
                          .order_by(EventLog.ts.desc()).limit(1)).first()
    if row is None:
        return None
    return {"ts": row.ts.isoformat() if row.ts else None, **(row.data or {})}


def clean(session: Session, report: Report | None = None) -> dict:
    """Remove what :func:`find` found. One line in the Activity says what left."""
    report = report or find(session)
    touched: dict[int, Schedule] = {}
    for video in report.local:
        if video.schedule is not None:
            touched[video.schedule.id] = video.schedule
        session.delete(video)
    session.flush()
    for item in report.library:
        path = Path(item.file_path)
        for video in list(item.videos):
            video.library_item_id = None
        session.delete(item)
        # The .txt or the cover may have arrived without the video: nothing to keep them for.
        queue.remove_sidecars(path)
        try:
            if path.parent != library_svc.root() and path.parent.is_dir() and not any(path.parent.iterdir()):
                path.parent.rmdir()
        except OSError:
            pass
    session.flush()
    for schedule in touched.values():
        scheduling.plan_next_slot(session, schedule)
    result = report.json()
    if result["total"]:
        events.record(
            session, "missing_cleaned",
            f"Sin archivo en disco: {len(report.local)} vídeo(s) quitados de {len(touched)} cola(s) y "
            f"{len(report.library)} entrada(s) de la biblioteca"
            + (f"; {len(report.zernio)} en manos de Zernio se quedan" if report.zernio else "") + ".",
            level="warn", data={k: result[k] for k in ("videos", "library", "queues", "folders")},
        )
    return result


def stuck_uploads(report: Report, timeout_minutes: int, now: datetime | None = None) -> list[VideoItem]:
    """Uploads that the app lost half-way (older than any real one): the file is gone, so they are
    counted with the ones in Zernio's hands but will never finish. Reported, not touched."""
    now = now or datetime.now(timezone.utc)
    out = []
    for v in report.zernio:
        if v.status != VideoStatus.UPLOADING:
            continue
        started = (v.upload_started_at or v.created_at).astimezone(timezone.utc)
        if now - started >= timedelta(minutes=timeout_minutes):
            out.append(v)
    return out
