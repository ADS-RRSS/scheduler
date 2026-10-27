"""Copied to another folder (another Mac): the video paths follow, and nothing else moves."""
from __future__ import annotations

from app.db import session_scope
from app.models import Account, EventLog, LibraryItem, Schedule, VideoItem, VideoStatus
from app.services import relocation


def test_only_paths_under_the_old_folder_move_and_the_activity_says_so(fresh_db, tmp_path):
    old, new = tmp_path / "antes" / "Scheduler", tmp_path / "ahora" / "Scheduler"
    paths = {
        "biblioteca.mp4": (old / "media" / "library" / "serie" / "biblioteca.mp4",
                           new / "media" / "library" / "serie" / "biblioteca.mp4"),
        "publicado.mp4": (old / "media" / "posted" / "publicado.mp4", new / "media" / "posted" / "publicado.mp4"),
        "fuera.mp4": (tmp_path / "otra" / "fuera.mp4", tmp_path / "otra" / "fuera.mp4"),
        "vecino.mp4": (tmp_path / "antes" / "Scheduler2" / "vecino.mp4",       # mismo prefijo de texto, otra carpeta
                       tmp_path / "antes" / "Scheduler2" / "vecino.mp4"),
    }
    with session_scope() as session:
        account = Account(external_id="zernio:acc1", zernio_account_id="acc1", username="c", display_name="c")
        session.add(account)
        session.flush()
        queue = Schedule(account_id=account.id, name="Cola")
        session.add(queue)
        session.flush()
        for i, (name, (before, _)) in enumerate(paths.items()):
            session.add(LibraryItem(folder="serie", file_path=str(before), original_name=name,
                                    checksum=f"sum-{i}", size_bytes=3))
            session.add(VideoItem(account_id=account.id, schedule_id=queue.id, file_path=str(before),
                                  original_name=name, checksum=f"sum-{i}", size_bytes=3,
                                  status=VideoStatus.PUBLISHED))

    with session_scope() as session:
        assert len(relocation.plan(session, old, new)) == 4
        assert relocation.relocate(session, old, new) == 4

    with session_scope() as session:
        for model in (LibraryItem, VideoItem):
            by_name = {r.original_name: r.file_path for r in session.query(model).all()}
            assert by_name == {name: str(after) for name, (_, after) in paths.items()}
        events = session.query(EventLog).filter_by(kind="install_relocated").all()
        assert len(events) == 1 and events[0].data["rows"] == 4
        # La segunda vez no hay nada que mover ni que apuntar.
        assert relocation.relocate(session, old, new) == 0
    with session_scope() as session:
        assert session.query(EventLog).filter_by(kind="install_relocated").count() == 1
