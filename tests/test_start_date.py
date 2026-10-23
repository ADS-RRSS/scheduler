"""«Empezar a publicar»: a queue that books nothing before a given day.

Every hour a queue gets comes from the same planner, so these tests check the
places that ask it: the hour pinned on the head, the preview, the calendar
and the handover to Zernio — all of them must start on that day.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.db import session_scope
from app.models import Schedule, VideoItem, VideoStatus
from tests.helpers import (
    default_queue_id,
    make_account,
    put_queue_schedule,
    reset_mock,
    upload_to_queue,
)

MADRID = ZoneInfo("Europe/Madrid")


def _local_today() -> date:
    return datetime.now(timezone.utc).astimezone(MADRID).date()


def _midnight(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=MADRID)


def _waiting(account_id: int, queue_id: int, n: int, tag: str) -> None:
    with session_scope() as session:
        for i in range(n):
            session.add(VideoItem(account_id=account_id, schedule_id=queue_id,
                                  file_path=f"/x/{tag}{i}.mp4", original_name=f"{tag}{i}.mp4",
                                  checksum=f"{tag}-{account_id}-{i}", status=VideoStatus.QUEUED))


async def test_the_first_hour_falls_on_the_start_day_inside_its_window(client):
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    _waiting(account_id, queue_id, 3, "start-window")
    start = _local_today() + timedelta(days=3)

    saved = await put_queue_schedule(client, queue_id, start_date=start.isoformat(),
                                     windows=[{"start": 600, "end": 720}])
    schedule = saved["schedule"]
    assert schedule["start_date"] == start.isoformat()
    first = datetime.fromisoformat(schedule["next_slot_at"]).astimezone(MADRID)
    assert first.date() == start and 10 <= first.hour < 12

    # The head carries that hour, so the worker has nothing to do until then.
    with session_scope() as session:
        head = session.get(Schedule, queue_id)
        pinned = [v.scheduled_at for v in head.videos if v.status == VideoStatus.SCHEDULED]
    assert pinned and pinned[0] >= _midnight(start)


async def test_preview_and_calendar_start_on_that_day(client):
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    _waiting(account_id, queue_id, 4, "start-preview")
    start = _local_today() + timedelta(days=2)
    await put_queue_schedule(client, queue_id, start_date=start.isoformat())

    preview = (await client.get(f"/api/schedules/{queue_id}/preview", params={"count": 5})).json()
    assert preview["slots"] and preview["slots"][0]["date"] == start.isoformat()
    assert all(datetime.fromisoformat(s["utc"]) >= _midnight(start) for s in preview["slots"])

    week = (await client.get("/api/calendar",
                             params={"start": _local_today().isoformat(), "days": 7})).json()
    mine = [m for m in week["marks"] if m["schedule_id"] == queue_id
            and m["kind"] in ("scheduled", "projected")]
    assert mine and all(m["day"] >= start.isoformat() for m in mine)


async def test_handover_to_zernio_dates_posts_from_the_start_day(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    start = _local_today() + timedelta(days=4)
    await put_queue_schedule(client, queue_id, start_date=start.isoformat(),
                             interval_min_minutes=30, interval_max_minutes=30)
    for i in range(2):
        copy = sample_video.with_name(f"start-{i}-{sample_video.name}")
        copy.write_bytes(sample_video.read_bytes() + bytes([i]))
        await upload_to_queue(client, queue_id, copy)
        copy.unlink(missing_ok=True)

    plan = (await client.get(f"/api/schedules/{queue_id}/delegation")).json()
    assert plan["count"] == 2
    assert plan["posts"][0]["date"] == start.isoformat()


async def test_now_clears_it_and_a_day_already_reached_means_now(client):
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    later = _local_today() + timedelta(days=5)
    await put_queue_schedule(client, queue_id, start_date=later.isoformat())

    # «Ahora» sends null.
    saved = await put_queue_schedule(client, queue_id, start_date=None)
    assert saved["schedule"]["start_date"] is None
    assert datetime.fromisoformat(saved["schedule"]["next_slot_at"]) < _midnight(later)

    # Today (or an old date) holds nothing back, and is dropped on planning.
    saved = await put_queue_schedule(client, queue_id, start_date=_local_today().isoformat())
    assert saved["schedule"]["start_date"] is None
    with session_scope() as session:
        assert session.get(Schedule, queue_id).start_date is None


async def test_moving_the_start_warns_about_posts_already_in_zernio(client):
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    with session_scope() as session:
        session.add(VideoItem(account_id=account_id, schedule_id=queue_id,
                              file_path="/x/parked.mp4", original_name="parked.mp4",
                              checksum=f"parked-{account_id}", status=VideoStatus.DELEGATED,
                              scheduled_at=datetime.now(timezone.utc) + timedelta(hours=5),
                              zernio_post_id="post_parked"))
    start = (_local_today() + timedelta(days=2)).isoformat()

    saved = await put_queue_schedule(client, queue_id, start_date=start)
    assert any("Zernio" in w for w in saved["warnings"])
    # Saving again without moving it says nothing more.
    saved = await put_queue_schedule(client, queue_id, start_date=start)
    assert not any("Zernio" in w for w in saved["warnings"])


async def test_a_copied_queue_starts_now(client):
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id,
                             start_date=(_local_today() + timedelta(days=3)).isoformat())
    resp = await client.post(f"/api/accounts/{account_id}/schedules",
                             json={"name": "Copia", "copy_from": queue_id})
    assert resp.status_code == 200, resp.text
    assert resp.json()["schedule"]["start_date"] is None


async def test_a_start_hour_holds_the_queue_until_that_minute(client):
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    _waiting(account_id, queue_id, 3, "start-hour")
    start = _local_today() + timedelta(days=2)

    saved = await put_queue_schedule(client, queue_id, start_date=start.isoformat(), start_time="18:30",
                                     windows=[{"start": 600, "end": 1380}])
    schedule = saved["schedule"]
    assert schedule["start_date"] == start.isoformat()
    assert schedule["start_time"] == "18:30"
    first = datetime.fromisoformat(schedule["next_slot_at"]).astimezone(MADRID)
    assert first >= datetime.combine(start, datetime.min.time().replace(hour=18, minute=30), tzinfo=MADRID)
    assert first.date() == start


async def test_today_at_a_later_hour_holds_and_an_hour_gone_means_now(client):
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    now = datetime.now(timezone.utc).astimezone(MADRID)

    gone = (now - timedelta(minutes=5))
    saved = await put_queue_schedule(client, queue_id, start_date=gone.date().isoformat(),
                                     start_time=f"{gone:%H:%M}")
    assert saved["schedule"]["start_date"] is None
    assert saved["schedule"]["start_time"] is None
    with session_scope() as session:
        stored = session.get(Schedule, queue_id)
        assert stored.start_date is None and stored.start_time is None

    later = now + timedelta(hours=2)
    saved = await put_queue_schedule(client, queue_id, start_date=later.date().isoformat(),
                                     start_time=f"{later:%H:%M}")
    assert saved["schedule"]["start_date"] == later.date().isoformat()
    assert saved["schedule"]["start_time"] == f"{later:%H:%M}"


async def test_an_hour_without_a_day_is_dropped(client):
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    saved = await put_queue_schedule(client, queue_id, start_date=None, start_time="10:00")
    assert saved["schedule"]["start_time"] is None
    with session_scope() as session:
        assert session.get(Schedule, queue_id).start_time is None
