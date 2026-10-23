"""The weekly calendar: real marks where they are, and what the rules would do next."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.db import session_scope
from app.models import Account, VideoItem, VideoStatus
from app.services import calendar
from tests.helpers import (
    ACCOUNT_2,
    add_queue,
    default_queue_id,
    make_account,
    put_queue_schedule,
)


def _rows(account_id: int, queue_id: int, *specs) -> None:
    with session_scope() as session:
        for i, (status, when) in enumerate(specs):
            item = VideoItem(account_id=account_id, schedule_id=queue_id,
                             file_path=f"/x/{account_id}-{queue_id}-{i}.mp4",
                             original_name=f"v{account_id}-{queue_id}-{i}.mp4",
                             checksum=f"{account_id}-{queue_id}-{i}", status=status)
            if status == VideoStatus.PUBLISHED:
                item.published_at = when
            else:
                item.scheduled_at = when
            if status == VideoStatus.DELEGATED:
                item.zernio_post_id = f"post_{i}"
            session.add(item)


def _waiting(account_id: int, queue_id: int, n: int, tag: str = "w") -> None:
    with session_scope() as session:
        for i in range(n):
            session.add(VideoItem(account_id=account_id, schedule_id=queue_id,
                                  file_path=f"/x/{tag}{i}.mp4", original_name=f"{tag}{i}.mp4",
                                  checksum=f"{tag}-{account_id}-{i}", status=VideoStatus.QUEUED))


async def test_real_marks_land_on_their_day_and_minute(client):
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, enabled=False)
    now = datetime.now(timezone.utc)
    _rows(account_id, queue_id,
          (VideoStatus.PUBLISHED, now - timedelta(hours=2)),
          (VideoStatus.DELEGATED, now + timedelta(hours=3)),
          (VideoStatus.FAILED, now - timedelta(hours=1)),
          (VideoStatus.DELEGATED, now + timedelta(days=30)))  # another month: not this week

    start = (now - timedelta(days=3)).date().isoformat()
    data = (await client.get("/api/calendar", params={"start": start})).json()
    kinds = sorted(m["kind"] for m in data["marks"])
    assert kinds == ["delegated", "failed", "published"]
    delegated = next(m for m in data["marks"] if m["kind"] == "delegated")
    local = datetime.fromisoformat(delegated["at"]).astimezone(ZoneInfo("Europe/Madrid"))
    assert delegated["minute"] == local.hour * 60 + local.minute and delegated["day"] == local.date().isoformat()
    assert len(data["days"]) == 7 and data["accounts"][0]["label"]
    # Failed ones are shown but not counted as a post of that day.
    assert sum(sum(per.values()) for per in data["totals"].values()) == 2


async def test_waiting_videos_are_projected_with_the_queue_rules(client):
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, interval_min_minutes=120, interval_max_minutes=120,
                             max_per_day=3)
    _waiting(account_id, queue_id, 12)
    # Pin the head the way the worker does.
    from app.services import scheduling
    with session_scope() as session:
        scheduling.plan_next_slot(session, session.get(Account, account_id).schedules[0])

    today = date.today()
    data = (await client.get("/api/calendar", params={"start": today.isoformat(), "days": 7})).json()
    marks = data["marks"]
    assert [m["kind"] for m in marks].count("scheduled") == 1
    projected = [m for m in marks if m["kind"] == "projected"]
    assert projected, data
    stamps = [datetime.fromisoformat(m["at"]) for m in marks if m["kind"] in ("scheduled", "projected")]
    assert all((b - a) >= timedelta(minutes=119) for a, b in zip(stamps, stamps[1:]))
    per_day = {}
    for m in marks:
        per_day[m["day"]] = per_day.get(m["day"], 0) + 1
    assert max(per_day.values()) <= 3, per_day
    # Only a simulation: nothing was written.
    with session_scope() as session:
        assert session.query(VideoItem).filter(VideoItem.status == VideoStatus.QUEUED).count() == 11

    plain = (await client.get("/api/calendar", params={"start": today.isoformat(),
                                                       "projected": "false"})).json()
    assert all(m["kind"] != "projected" for m in plain["marks"])


async def test_two_queues_of_one_account_do_not_overlap_in_the_projection(client):
    account_id = await make_account(client)
    first = await default_queue_id(client, account_id)
    second = await add_queue(client, account_id, "Noche")
    for q in (first, second):
        await put_queue_schedule(client, q, interval_min_minutes=30, interval_max_minutes=30,
                                 max_per_day=None)
    await client.patch(f"/api/accounts/{account_id}", json={"min_gap_minutes": 20, "max_per_day": 10})
    _waiting(account_id, first, 8, "a")
    _waiting(account_id, second, 8, "b")

    data = (await client.get("/api/calendar", params={"start": date.today().isoformat()})).json()
    stamps = sorted(datetime.fromisoformat(m["at"]) for m in data["marks"])
    assert len(stamps) >= 10
    assert all((b - a) >= timedelta(minutes=20) for a, b in zip(stamps, stamps[1:])), \
        "las dos colas respetan la separación de la cuenta entre sí"
    per_day = {}
    for m in data["marks"]:
        per_day[m["day"]] = per_day.get(m["day"], 0) + 1
    assert max(per_day.values()) <= 10, "el máximo diario de la cuenta vale para las dos"


async def test_filters_and_notes(client):
    tiktok = await make_account(client)
    other = await make_account(client, ACCOUNT_2)
    queue_id = await default_queue_id(client, other)
    await put_queue_schedule(client, queue_id, enabled=False)
    _waiting(other, queue_id, 2, "z")

    only = (await client.get("/api/calendar", params={"account_id": tiktok})).json()
    assert [a["id"] for a in only["accounts"]] == [tiktok]
    everyone = (await client.get("/api/calendar")).json()
    assert {a["id"] for a in everyone["accounts"]} == {tiktok, other}
    assert any("parada" in n for n in everyone["notes"])
    assert (await client.get("/api/calendar", params={"platform": "youtube"})).json()["accounts"] == []
    assert (await client.get("/api/calendar", params={"start": "ayer"})).status_code == 400
    assert calendar.week_start(date(2026, 10, 1)) == date(2026, 9, 28)
