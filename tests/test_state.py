"""/api/state at the size of a real account: nothing on its way is ever left out."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import event

from app.db import engine, session_scope
from app.models import Comment, VideoItem, VideoStatus
from tests.helpers import account_entry, default_queue_id, make_account

LIVE = 520
PUBLISHED = 340
FAILED = 5


def _fill(account_id: int, queue_id: int) -> None:
    """Hundreds of rows straight into the database: uploading them would take minutes."""
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(PUBLISHED):
        rows.append(VideoItem(account_id=account_id, schedule_id=queue_id,
                              file_path=f"/x/p{i}.mp4", original_name=f"p{i}.mp4",
                              checksum=f"p{i}", status=VideoStatus.PUBLISHED,
                              published_at=now - timedelta(hours=i), transcript="palabra " * 200))
    for i in range(FAILED):
        rows.append(VideoItem(account_id=account_id, schedule_id=queue_id,
                              file_path=f"/x/f{i}.mp4", original_name=f"f{i}.mp4",
                              checksum=f"f{i}", status=VideoStatus.FAILED))
    # The oldest rows still to go out come last: a cap by newest id cut exactly these.
    for i in range(LIVE):
        status = VideoStatus.DELEGATED if i % 2 else VideoStatus.QUEUED
        rows.append(VideoItem(account_id=account_id, schedule_id=queue_id,
                              file_path=f"/x/l{i}.mp4", original_name=f"l{i}.mp4",
                              checksum=f"l{i}", status=status,
                              zernio_post_id=f"post_{i}" if i % 4 == 1 else None,
                              scheduled_at=now + timedelta(hours=i)))
    with session_scope() as session:
        session.add_all(rows)
        session.flush()
        newest = session.query(VideoItem).filter_by(checksum=f"p{PUBLISHED - 1}").one()
        session.add_all([Comment(video_id=newest.id, account_id=account_id, external_id=f"c{i}",
                                 message="hola") for i in range(3)])


async def test_every_video_on_its_way_is_listed_and_counted(client):
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    _fill(account_id, queue_id)

    entry = await account_entry(client, account_id)
    videos = entry["videos"]
    live = [v for v in videos if v["status"] in ("QUEUED", "DELEGATED")]
    assert len(live) == LIVE, "ningún vídeo pendiente o en Zernio puede faltar"

    # The history is cut at the latest ones, and says so.
    history = entry["history"]
    assert history == {"shown": 300, "total": PUBLISHED + FAILED, "limit": 300}
    assert sum(v["status"] in ("PUBLISHED", "FAILED") for v in videos) == 300

    # The counters are the database's, not the list's.
    stats = entry["stats"]
    assert stats["published"] == PUBLISHED and stats["failed"] == FAILED
    assert stats["queued"] == LIVE // 2 and stats["delegated"] == LIVE // 2
    assert stats["handover_pending"] == LIVE // 2 - LIVE // 4
    assert entry["queues"][0]["stats"] == stats

    # Light rows: no transcript, but the comment count is right.
    published = next(v for v in videos if v["original_name"] == f"p{PUBLISHED - 1}.mp4")
    assert "transcript" not in published and published["has_transcript"] is True
    assert len(published["transcript_excerpt"]) <= 280
    assert published["comment_count"] == 3
    full = (await client.get(f"/api/videos/{published['id']}")).json()
    assert full["transcript"].startswith("palabra")


async def test_the_state_costs_the_same_queries_whatever_the_size(client):
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await client.get("/api/state")  # warm up: settings rows, probes

    counted = {"n": 0}

    def count(*_args, **_kwargs):
        counted["n"] += 1

    event.listen(engine, "before_cursor_execute", count)
    try:
        await client.get("/api/state")
        empty = counted["n"]
        _fill(account_id, queue_id)
        counted["n"] = 0
        await client.get("/api/state")
        full = counted["n"]
    finally:
        event.remove(engine, "before_cursor_execute", count)
    # It used to cost two queries per video (its numbers and its comments).
    assert full <= empty + 6, (empty, full)
