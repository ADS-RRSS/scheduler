"""«Publicar ya, fuera de turno»: from the library straight out, on top of the queue's rhythm."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db import session_scope
from app.models import Schedule, VideoItem, VideoStatus
from app.scheduler.planner import OPENING_SPREAD_MINUTES
from app.services import delegation
from tests.helpers import (
    ACCOUNT_2,
    account_entry,
    default_queue_id,
    drain,
    enqueue,
    make_account,
    mock_state,
    put_schedule,
    reset_mock,
    upload_to_library,
)


def _variant(sample, name: str, tail: bytes):
    target = sample.parent / name
    target.write_bytes(sample.read_bytes() + tail)
    return target


async def _publish_now(client, queue_id: int, library_id: int):
    return await client.post(f"/api/schedules/{queue_id}/videos",
                             json={"library_ids": [library_id], "publish_now": True})


def _video(name: str) -> VideoItem:
    with session_scope() as session:
        return next(v for v in session.query(VideoItem).all() if v.original_name == name)


async def test_it_goes_out_now_and_the_queue_keeps_its_rhythm(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    # A fixed interval: every replan draws the same hour, so a moved hour can
    # only mean the extra moved the cadence.
    await put_schedule(client, account_id, interval_min_minutes=30, interval_max_minutes=30)
    queue_id = await default_queue_id(client, account_id)

    # One post already out (the cadence counts from it) and one waiting its turn.
    first = await upload_to_library(client, _variant(sample_video, "primero.mp4", b"\x01"))
    await enqueue(client, queue_id, first["id"])
    await drain(account_id)
    waiting = await upload_to_library(client, _variant(sample_video, "siguiente.mp4", b"\x02"))
    await enqueue(client, queue_id, waiting["id"])
    with session_scope() as session:
        last_post = session.get(Schedule, queue_id).last_post_at
    slot = _video("siguiente.mp4").scheduled_at
    assert last_post is not None and slot > datetime.now(timezone.utc) + timedelta(minutes=20)

    extra = await upload_to_library(client, _variant(sample_video, "urgente.mp4", b"\x03"))
    resp = await _publish_now(client, queue_id, extra["id"])
    assert resp.status_code == 200, resp.text
    assert "fuera de turno" in resp.json()["message"]
    (added,) = resp.json()["added"]
    assert added["extra"] is True and added["status"] == "SCHEDULED"
    assert _video("siguiente.mp4").scheduled_at == slot, "el siguiente no pierde su hora"

    await drain(account_id)
    assert _video("urgente.mp4").status == VideoStatus.PUBLISHED
    assert len((await mock_state())["posts"]) == 2
    # Nothing moved: not the cadence, not the next video's hour.
    with session_scope() as session:
        assert session.get(Schedule, queue_id).last_post_at == last_post
    assert _video("siguiente.mp4").scheduled_at == slot
    assert _video("siguiente.mp4").status == VideoStatus.SCHEDULED

    # It stays in the queue, so it can never be queued (and published) again.
    again = await _publish_now(client, queue_id, extra["id"])
    assert again.json()["added"] == [] and "ya está" in again.json()["problems"][0]


async def test_it_fills_no_day_of_the_budget_and_ignores_a_stopped_queue(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    await put_schedule(client, account_id, enabled=False, max_per_day=1)
    queue_id = await default_queue_id(client, account_id)

    item = await upload_to_library(client, sample_video)
    await _publish_now(client, queue_id, item["id"])
    await drain(account_id)
    assert _video(sample_video.name).status == VideoStatus.PUBLISHED

    with session_scope() as session:
        from zoneinfo import ZoneInfo

        from app.services import queue
        today = datetime.now(ZoneInfo("Europe/Madrid")).date()
        assert queue.committed_on(session, today, ZoneInfo("Europe/Madrid"),
                                  account_id=account_id) == 0
        assert queue.committed_instants(session, account_id) == []


async def test_one_video_to_two_accounts_at_once(client, sample_video):
    await reset_mock()
    tiktok = await make_account(client)
    other = await make_account(client, ACCOUNT_2)
    item = await upload_to_library(client, sample_video)
    for account_id in (tiktok, other):
        resp = await _publish_now(client, await default_queue_id(client, account_id), item["id"])
        assert len(resp.json()["added"]) == 1
    for account_id in (tiktok, other):
        await drain(account_id)
    assert len((await mock_state())["posts"]) == 2
    for account_id in (tiktok, other):
        (video,) = (await account_entry(client, account_id))["videos"]
        assert video["status"] == "PUBLISHED" and video["extra"] is True


async def test_a_delegation_leaves_it_alone(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    queue_id = await default_queue_id(client, account_id)
    normal = await upload_to_library(client, _variant(sample_video, "normal.mp4", b"\x04"))
    await enqueue(client, queue_id, normal["id"])
    extra = await upload_to_library(client, _variant(sample_video, "urgente.mp4", b"\x05"))
    await _publish_now(client, queue_id, extra["id"])

    with session_scope() as session:
        planned = delegation.plan(session, session.get(Schedule, queue_id))
        assert [p.item.original_name for p in planned] == ["normal.mp4"]


async def test_an_inactive_account_is_refused(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    with session_scope() as session:
        from app.models import Account
        session.get(Account, account_id).is_active = False
    item = await upload_to_library(client, sample_video)
    resp = await _publish_now(client, await default_queue_id(client, account_id), item["id"])
    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# «Programar fecha y hora»: straight to Zernio for an instant the user picks
# --------------------------------------------------------------------------- #

async def _publish_at(client, queue_id: int, library_id: int, when: datetime):
    return await client.post(f"/api/schedules/{queue_id}/videos",
                             json={"library_ids": [library_id], "publish_at": when.isoformat()})


def _same_opening(new: datetime, old: datetime) -> bool:
    """A video waiting for a day's opening draws its minute in the opening spread
    again on every replan; what must hold is that it waits for the same opening."""
    return abs(new - old) <= timedelta(minutes=OPENING_SPREAD_MINUTES)


async def test_a_chosen_hour_goes_to_zernio_and_the_queue_keeps_its_rhythm(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    await put_schedule(client, account_id, interval_min_minutes=30, interval_max_minutes=30,
                       max_per_day=1)
    queue_id = await default_queue_id(client, account_id)
    # One post out today, so the next one waits for tomorrow (cap of one a day).
    first = await upload_to_library(client, _variant(sample_video, "primero.mp4", b"\x08"))
    await enqueue(client, queue_id, first["id"])
    await drain(account_id)
    waiting = await upload_to_library(client, _variant(sample_video, "siguiente.mp4", b"\x06"))
    await enqueue(client, queue_id, waiting["id"])
    slot = _video("siguiente.mp4").scheduled_at
    assert slot > datetime.now(timezone.utc) + timedelta(minutes=20)

    # The same day as the queue's next video: a chosen hour that took a day of
    # the budget would push that video to the day after.
    when = (slot + timedelta(hours=1)).replace(second=0, microsecond=0)
    extra = await upload_to_library(client, _variant(sample_video, "programado.mp4", b"\x07"))
    resp = await _publish_at(client, queue_id, extra["id"], when)
    assert resp.status_code == 200, resp.text
    assert "programados en Zernio" in resp.json()["message"]
    (added,) = resp.json()["added"]
    assert added["extra"] is True and added["status"] == "DELEGATED"
    assert _same_opening(_video("siguiente.mp4").scheduled_at, slot), "el siguiente no pierde su hora"

    await drain(account_id)
    posts = [p for p in (await mock_state())["posts"].values()
             if p["publish_now"] is False]
    (post,) = posts
    assert datetime.fromisoformat(post["scheduled_for"].replace("Z", "+00:00")) == when
    video = _video("programado.mp4")
    assert video.status == VideoStatus.DELEGATED and video.zernio_post_id
    # Outside the budget: the daily cap of one still has room for the queue's own.
    assert _same_opening(_video("siguiente.mp4").scheduled_at, slot)


async def test_a_chosen_hour_in_the_past_or_without_zone_is_refused(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    item = await upload_to_library(client, sample_video)
    gone = await _publish_at(client, queue_id, item["id"],
                             datetime.now(timezone.utc) - timedelta(minutes=5))
    assert gone.status_code == 400
    naive = await client.post(f"/api/schedules/{queue_id}/videos",
                              json={"library_ids": [item["id"]], "publish_at": "2030-01-01T10:00:00"})
    assert naive.status_code == 422
    both = await client.post(f"/api/schedules/{queue_id}/videos", json={
        "library_ids": [item["id"]], "publish_now": True,
        "publish_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
    assert both.status_code == 400


async def test_a_chosen_hour_recovered_from_zernio_waits_its_turn(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    item = await upload_to_library(client, sample_video)
    await _publish_at(client, queue_id, item["id"], datetime.now(timezone.utc) + timedelta(days=1))
    await drain(account_id)
    video = _video(sample_video.name)
    resp = await client.post(f"/api/videos/{video.id}/undelegate")
    assert resp.status_code == 200, resp.text
    video = _video(sample_video.name)
    assert video.status in (VideoStatus.QUEUED, VideoStatus.SCHEDULED) and video.extra is False
