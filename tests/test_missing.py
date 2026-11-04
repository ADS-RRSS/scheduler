"""The database came from another Mac without its videos: what the queues and the library let go of."""
from __future__ import annotations

from pathlib import Path

from app.db import session_scope
from app.models import Account, EventLog, LibraryItem, Schedule, VideoItem, VideoStatus
from app.services import missing


def _seed(root: Path) -> dict[str, int]:
    """Two queues of one account. Half the rows point at files that exist (made here), half at files
    that never arrived (made on the other Mac). Returns name → video id."""
    ids = {}
    with session_scope() as session:
        account = Account(external_id="zernio:acc1", zernio_account_id="acc1", username="c", display_name="Cuenta")
        session.add(account)
        session.flush()
        q1, q2 = Schedule(account_id=account.id, name="Principal"), Schedule(account_id=account.id, name="Noche")
        session.add_all([q1, q2])
        session.flush()
        rows = [
            # name, exists, status, post id, queue
            ("aqui-cola.mp4", True, VideoStatus.QUEUED, None, q1),
            ("aqui-publicado.mp4", True, VideoStatus.PUBLISHED, "p1", q1),
            ("alla-cola.mp4", False, VideoStatus.QUEUED, None, q1),
            ("alla-programado.mp4", False, VideoStatus.SCHEDULED, None, q2),
            ("alla-marcado.mp4", False, VideoStatus.DELEGATED, None, q2),        # marked, not in Zernio yet
            ("alla-zernio.mp4", False, VideoStatus.DELEGATED, "z9", q2),         # parked in Zernio: stays
            ("alla-publicado.mp4", False, VideoStatus.PUBLISHED, "p2", q1),      # history: stays
            ("alla-fallido.mp4", False, VideoStatus.FAILED, None, q1),           # history: stays
        ]
        for i, (name, exists, status, post, queue) in enumerate(rows):
            folder = "aqui" if exists else "alla"
            path = root / folder / name
            if exists:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"mp4")
            item = LibraryItem(folder=folder, file_path=str(path), original_name=name, checksum=f"sum-{i}",
                               size_bytes=3)
            session.add(item)
            session.flush()
            video = VideoItem(account_id=account.id, schedule_id=queue.id, library_item_id=item.id,
                              file_path=str(path), original_name=name, checksum=f"sum-{i}", size_bytes=3,
                              status=status, zernio_post_id=post)
            session.add(video)
            session.flush()
            ids[name] = video.id
    return ids


def test_only_local_rows_without_a_file_leave(fresh_db, tmp_root):
    root = Path(tmp_root) / "library"
    _seed(root)
    # A stray .txt that came without its video goes with the library entry.
    (root / "alla").mkdir(parents=True, exist_ok=True)
    (root / "alla" / "alla-cola.txt").write_text("ficha")

    with session_scope() as session:
        report = missing.find(session)
        got = report.json()
        assert got["videos"] == {"local": 3, "zernio": 1, "history": 2}
        assert got["intact"] == {"videos": 2, "library": 2}
        # The library entry parked in Zernio stays (recovering the post would need it); the rest without a
        # file go, history included: published and failed rows just stop pointing at the library.
        assert got["library"] == 5 and got["folders"] == {"alla": 5}
        assert [(q["name"], q["count"]) for q in got["queues"]] == [("Noche", 2), ("Principal", 1)]
        assert got["total"] == 8
        result = missing.clean(session, report)
        assert result["total"] == 8

    with session_scope() as session:
        left = {v.original_name: v for v in session.query(VideoItem).all()}
        assert set(left) == {"aqui-cola.mp4", "aqui-publicado.mp4", "alla-zernio.mp4", "alla-publicado.mp4",
                             "alla-fallido.mp4"}
        assert left["alla-zernio.mp4"].library_item_id is not None           # its entry stayed
        assert left["alla-publicado.mp4"].library_item_id is None            # detached from a gone entry
        assert left["aqui-cola.mp4"].library_item_id is not None
        assert {i.original_name for i in session.query(LibraryItem).all()} == {
            "aqui-cola.mp4", "aqui-publicado.mp4", "alla-zernio.mp4"}
        ev = session.query(EventLog).filter_by(kind="missing_cleaned").one()
        assert ev.level == "warn" and "3 vídeo(s)" in ev.message and "1 en manos de Zernio" in ev.message
        assert ev.data["queues"][0]["count"] == 2
        # The queues were replanned: the head of each one has its slot again.
        for q in session.query(Schedule).all():
            heads = [v for v in q.videos if v.status in (VideoStatus.QUEUED, VideoStatus.SCHEDULED)]
            assert all(v.scheduled_at is not None for v in heads)
        assert not (root / "alla" / "alla-cola.txt").exists()
    # Nothing left to do: the second pass is empty and leaves no trace.
    with session_scope() as session:
        assert missing.clean(session)["total"] == 0
        assert session.query(EventLog).filter_by(kind="missing_cleaned").count() == 1


def test_nothing_missing_nothing_touched(fresh_db, tmp_root):
    root = Path(tmp_root) / "library"
    with session_scope() as session:
        account = Account(external_id="zernio:acc1", zernio_account_id="acc1", username="c", display_name="c")
        session.add(account)
        session.flush()
        path = root / "serie" / "v.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        session.add(VideoItem(account_id=account.id, file_path=str(path), original_name="v.mp4", checksum="s",
                              size_bytes=1, status=VideoStatus.QUEUED))
    with session_scope() as session:
        assert missing.find(session).json()["total"] == 0
        assert missing.clean(session)["total"] == 0
        assert session.query(VideoItem).count() == 1


async def test_the_admin_endpoints_preview_then_clean_with_a_backup(client, tmp_root):
    _seed(Path(tmp_root) / "library")
    before = (await client.get("/api/admin/videos/missing")).json()
    assert before["total"] == 8 and before["relocated"] is None and before["stuck_uploads"] == 0
    resp = await client.post("/api/admin/videos/missing/clean")
    assert resp.status_code == 200
    body = resp.json()
    assert body["videos"]["local"] == 3 and body["library"] == 5 and body["backup"]
    assert "Copia previa" in body["message"]
    assert (await client.get("/api/admin/videos/missing")).json()["total"] == 0
    # Running it again changes nothing and makes no backup.
    again = (await client.post("/api/admin/videos/missing/clean")).json()
    assert again["total"] == 0 and again["backup"] is None
    kinds = [e["kind"] for e in (await client.get("/api/events")).json()["events"]]
    assert kinds.count("missing_cleaned") == 1
