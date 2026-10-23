"""Nothing may keep SQLite's write lock while it waits on the network.

SQLite lets one writer through at a time. A transaction that has written and
then awaits Zernio (analytics pages, the next video's comments, a webhook)
keeps every other writer waiting — the UI, the caption worker, the publisher —
and, since those writes run on the event loop, freezes the whole app for up to
the 30 s busy timeout. Each test pauses inside the network call and checks
that another connection can still write.
"""
from __future__ import annotations

import shutil
import sqlite3

from app.config import get_settings
from app.services import accounts as accounts_svc
from app.services import publisher
from app.services.zernio import CommentItem, PostMetrics
from tests.helpers import (
    ACCOUNT_1,
    ACCOUNT_2,
    drain,
    first_video,
    make_account,
    put_schedule,
    reset_mock,
    upload_sample,
)


def _can_write() -> bool:
    """Can a second connection take the write lock right now?"""
    con = sqlite3.connect(get_settings().database_path, timeout=0.2)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


async def _publish(client, account_id: int, path) -> dict:
    video = await upload_sample(client, account_id, path)
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)
    return video


class _FakeClient:
    """Stands in for Zernio and probes the lock in the middle of each call."""

    def __init__(self):
        self.probes: list[bool] = []

    async def fetch_comments(self, ref, account_id, limit=50):
        self.probes.append(_can_write())
        return [CommentItem(external_id=f"{ref}-c1", message="hola", author_username="fan")]


async def test_analytics_pages_and_accounts_do_not_hold_the_write_lock(client, sample_video, monkeypatch):
    await reset_mock()
    for zernio_id in (ACCOUNT_1, ACCOUNT_2):            # the lock used to leak from one account to the next
        account_id = await make_account(client, zernio_id=zernio_id)
        await put_schedule(client, account_id)
        await _publish(client, account_id, sample_video)

    from app.db import session_scope
    from app.models import VideoItem, VideoStatus
    with session_scope() as session:
        post_ids = [v.zernio_post_id for v in session.query(VideoItem).filter(
            VideoItem.status == VideoStatus.PUBLISHED)]
    assert len(post_ids) == 2

    class _Both(_FakeClient):
        async def fetch_analytics(self, *, page: int, limit: int, account_id: str, **_):
            self.probes.append(_can_write())
            if page == 1:
                return [PostMetrics(post_id=p, views=10, likes=1) for p in post_ids] + [
                    PostMetrics(post_id=f"other-{i}") for i in range(limit - 2)]
            return []

    fake = _Both()
    monkeypatch.setattr(accounts_svc, "client", lambda account, settings: fake)

    resp = await client.post("/api/analytics/refresh")
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 2
    assert len(fake.probes) == 4 and all(fake.probes), fake.probes     # 2 accounts × 2 pages


async def test_comment_sync_does_not_hold_the_write_lock_between_videos(
        client, sample_video, tmp_path, monkeypatch):
    await reset_mock()
    account_id = await make_account(client)
    await put_schedule(client, account_id, min_interval_minutes=1, max_interval_minutes=1)
    other = tmp_path / "otro.mp4"
    shutil.copyfile(sample_video, other)
    with open(other, "ab") as f:                       # same clip, another checksum
        f.write(b"\0" * 32)
    await _publish(client, account_id, sample_video)
    await _publish(client, account_id, other)

    fake = _FakeClient()
    monkeypatch.setattr(accounts_svc, "client", lambda account, settings: fake)
    resp = await client.post("/api/comments/refresh")
    assert resp.status_code == 200, resp.text
    assert len(fake.probes) == 2, fake.probes           # one call per published video
    assert all(fake.probes), fake.probes


async def test_the_webhook_is_not_sent_inside_a_write(client, sample_video, monkeypatch):
    await reset_mock()
    probes: list[bool] = []

    async def announce(item, account, settings, url):
        probes.append(_can_write())

    monkeypatch.setattr(publisher, "announce", announce)
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    await _publish(client, account_id, sample_video)
    assert (await first_video(client))["status"] == "PUBLISHED"
    assert probes and all(probes), probes
