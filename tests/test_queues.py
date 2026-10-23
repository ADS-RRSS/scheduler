"""Several queues ("colas") on one TikTok account.

One account, one Zernio connection, but as many independent queues as needed:
each with its own videos, hours and cadence — mañana y noche, for instance.
What they must never do is forget they share one TikTok account.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.helpers import (
    ACCOUNT_1,
    ZERNIO_KEY,
    account_entry,
    add_queue,
    configure_mock,
    default_queue_id,
    drain,
    enqueue,
    make_account,
    put_queue_schedule,
    reset_mock,
    upload_to_queue,
)

DAY = [{"start": 600, "end": 1320}]    # 10:00 – 22:00
NIGHT = [{"start": 1320, "end": 600}]  # 22:00 – 10:00


async def _two_queues(client) -> tuple[int, int, int]:
    """An account with a 10-22 queue and a 22-10 one. Returns the three ids."""
    account_id = await make_account(client, ACCOUNT_1)
    day = await default_queue_id(client, account_id)
    await put_queue_schedule(client, day, name="Día", windows=DAY)
    night = await add_queue(client, account_id, name="Noche", copy_from=day)
    await put_queue_schedule(client, night, name="Noche", windows=NIGHT)
    return account_id, day, night


# --------------------------------------------------------------------------- #
# Creating, naming, ordering
# --------------------------------------------------------------------------- #

async def test_an_account_starts_with_one_queue(client):
    await reset_mock()
    account_id = await make_account(client)
    entry = await account_entry(client, account_id)
    assert len(entry["queues"]) == 1
    assert entry["queues"][0]["schedule"]["name"] == "Cola principal"
    assert entry["queues"][0]["schedule"]["position"] == 0
    assert entry["account"]["queue_count"] == 1


async def test_a_second_queue_is_added_and_kept_apart(client):
    await reset_mock()
    account_id, day, night = await _two_queues(client)

    entry = await account_entry(client, account_id)
    assert [q["schedule"]["name"] for q in entry["queues"]] == ["Día", "Noche"]
    assert [q["schedule"]["windows"] for q in entry["queues"]] == [DAY, NIGHT]
    assert entry["account"]["queue_count"] == 2


async def test_a_new_queue_can_copy_an_existing_one(client):
    await reset_mock()
    account_id = await make_account(client)
    first = await default_queue_id(client, account_id)
    await put_queue_schedule(
        client, first, interval_min_minutes=120, interval_max_minutes=180,
        max_per_day=3, disable_stitch=True, timezone="America/Bogota",
    )
    second = await add_queue(client, account_id, name="Copia", copy_from=first)

    entry = await account_entry(client, account_id)
    copy = next(q["schedule"] for q in entry["queues"] if q["schedule"]["id"] == second)
    assert copy["interval_min_minutes"] == 120 and copy["interval_max_minutes"] == 180
    assert copy["max_per_day"] == 3 and copy["disable_stitch"] is True
    assert copy["timezone"] == "America/Bogota"
    # Copied rules, but not the switch that makes it publish.
    assert copy["enabled"] is False


async def test_repeated_names_are_disambiguated(client):
    await reset_mock()
    account_id = await make_account(client)
    first = await default_queue_id(client, account_id)
    await put_queue_schedule(client, first, name="Noche")
    second = await add_queue(client, account_id, name="Noche")

    entry = await account_entry(client, account_id)
    names = [q["schedule"]["name"] for q in entry["queues"]]
    assert names == ["Noche", "Noche (2)"]
    # Renaming a queue to the name it already has is not a collision.
    resp = await client.patch(f"/api/schedules/{second}", json={"name": "Noche (2)"})
    assert resp.status_code == 200 and resp.json()["schedule"]["name"] == "Noche (2)"


async def test_queues_can_be_reordered(client):
    await reset_mock()
    account_id, day, night = await _two_queues(client)
    resp = await client.put(f"/api/accounts/{account_id}/schedules/order",
                            json={"order": [night, day]})
    assert resp.status_code == 200
    entry = await account_entry(client, account_id)
    assert [q["schedule"]["name"] for q in entry["queues"]] == ["Noche", "Día"]


async def test_the_same_tiktok_account_is_still_connected_once(client):
    """The answer to "can I connect it twice?" is: no, add a queue instead."""
    await reset_mock()
    await make_account(client, ACCOUNT_1)
    resp = await client.post("/api/accounts",
                             json={"api_key": ZERNIO_KEY, "account_ids": [ACCOUNT_1]})
    assert resp.status_code == 409
    assert "segunda cola" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# Each queue holds its own videos
# --------------------------------------------------------------------------- #

def _clip(video: Path, name: str, extra: bytes = b"") -> Path:
    """The sample under another name; ``extra`` bytes make it another video."""
    copy = video.with_name(name)
    copy.write_bytes(video.read_bytes() + extra)
    return copy


async def test_videos_are_added_into_one_queue(client, sample_video):
    await reset_mock()
    account_id, day, night = await _two_queues(client)
    await upload_to_queue(client, night, _clip(sample_video, "noche.mp4"))
    await upload_to_queue(client, day, _clip(sample_video, "dia.mp4", b"\x00"))

    entry = await account_entry(client, account_id)
    by_queue = {v["queue_name"]: v["original_name"] for v in entry["videos"]}
    assert by_queue == {"Noche": "noche.mp4", "Día": "dia.mp4"}
    counts = {q["schedule"]["name"]: q["stats"]["queued"] for q in entry["queues"]}
    assert counts == {"Día": 1, "Noche": 1}
    # The account's own numbers are the sum of its queues.
    assert entry["stats"]["queued"] == 2


async def test_the_same_file_cannot_sit_in_two_queues_of_one_account(client, sample_video):
    await reset_mock()
    _, day, night = await _two_queues(client)
    video = await upload_to_queue(client, day, sample_video)
    resp = await enqueue(client, night, video["library_item_id"])
    assert resp.json()["added"] == []
    assert "ya está" in resp.json()["problems"][0]
    assert "Día" in resp.json()["problems"][0], "debe decir en qué cola está ya"


async def test_a_video_can_be_moved_between_queues(client, sample_video):
    await reset_mock()
    account_id, day, night = await _two_queues(client)
    video = await upload_to_queue(client, day, sample_video)

    moved = await client.patch(f"/api/videos/{video['id']}", json={"schedule_id": night})
    assert moved.status_code == 200
    assert moved.json()["schedule_id"] == night and moved.json()["account_id"] == account_id

    entry = await account_entry(client, account_id)
    counts = {q["schedule"]["name"]: q["stats"]["queued"] for q in entry["queues"]}
    assert counts == {"Día": 0, "Noche": 1}


# --------------------------------------------------------------------------- #
# Each queue plans its own slots, inside the account's limits
# --------------------------------------------------------------------------- #

async def test_each_queue_plans_inside_its_own_window(client, sample_video):
    await reset_mock()
    account_id, day, night = await _two_queues(client)
    await put_queue_schedule(client, day, name="Día", windows=DAY, enabled=True)
    await put_queue_schedule(client, night, name="Noche", windows=NIGHT, enabled=True)

    for queue_id, slots in ((day, DAY[0]), (night, NIGHT[0])):
        preview = (await client.get(f"/api/schedules/{queue_id}/preview?count=6")).json()
        assert preview["slots"], "cada cola debe encontrar huecos"
        for slot in preview["slots"]:
            minutes = int(slot["local"][-5:-3]) * 60 + int(slot["local"][-2:])
            inside = (slots["start"] <= minutes < slots["end"] if slots["start"] < slots["end"]
                      else minutes >= slots["start"] or minutes < slots["end"])
            assert inside, f"{slot['local']} está fuera de la franja {slots}"


async def test_the_account_daily_cap_is_shared_by_every_queue(client):
    """Two queues of 5/day on an account capped at 6 give 6 posts, not 10."""
    await reset_mock()
    account_id, day, night = await _two_queues(client)
    await client.patch(f"/api/accounts/{account_id}", json={"max_per_day": 6})
    for queue_id in (day, night):
        await put_queue_schedule(
            client, queue_id, windows=[], max_per_day=5, enabled=True,
            interval_min_minutes=30, interval_max_minutes=30, min_gap_minutes=0,
            jitter_seconds=0,
        )

    slots = (await client.get(f"/api/schedules/{day}/preview?count=12")).json()["slots"]
    today = slots[0]["date"]
    assert sum(1 for s in slots if s["date"] == today) <= 6


async def test_the_account_cap_counts_what_the_other_queue_booked(client, sample_video):
    await reset_mock()
    account_id, day, night = await _two_queues(client)
    await client.patch(f"/api/accounts/{account_id}", json={"max_per_day": 1})
    for queue_id in (day, night):
        await put_queue_schedule(client, queue_id, windows=[], max_per_day=1, enabled=True)

    # One video booked in the day queue eats the account's whole budget, so the
    # night queue has to wait for tomorrow.
    await upload_to_queue(client, day, sample_video)
    entry = await account_entry(client, account_id)
    booked = next(q for q in entry["queues"] if q["schedule"]["id"] == day)
    assert booked["schedule"]["next_slot_at"] is not None

    slots = (await client.get(f"/api/schedules/{night}/preview?count=1")).json()["slots"]
    assert slots and slots[0]["date"] > booked["schedule"]["next_slot_at"][:10] or not slots


async def test_the_account_gap_keeps_two_queues_from_firing_together(client, sample_video):
    await reset_mock()
    account_id, day, night = await _two_queues(client)
    await client.patch(f"/api/accounts/{account_id}",
                       json={"min_gap_minutes": 180, "max_per_day": None})
    for queue_id in (day, night):
        await put_queue_schedule(
            client, queue_id, windows=[], enabled=True, min_gap_minutes=0, jitter_seconds=0,
            interval_min_minutes=1, interval_max_minutes=1,
        )

    await upload_to_queue(client, day, sample_video)
    await drain(account_id)

    entry = await account_entry(client, account_id)
    assert entry["videos"][0]["status"] == "PUBLISHED"
    published_at = datetime.fromisoformat(entry["videos"][0]["published_at"])
    for queue in entry["queues"]:
        slot = queue["schedule"]["next_slot_at"]
        if slot:
            assert datetime.fromisoformat(slot) >= published_at + timedelta(minutes=179), \
                f"la cola «{queue['schedule']['name']}» ignora el hueco de la cuenta"


async def _fill(client, queue_id: int, video: Path, count: int, tag: str) -> None:
    """``count`` copies of the sample clip, each with its own bytes.

    One account never takes the same file twice, so no two copies may match.
    """
    for i in range(count):
        copy = video.with_name(f"{tag}-{i}-{video.name}")
        copy.write_bytes(video.read_bytes() + tag.encode() + bytes([i]))
        await upload_to_queue(client, queue_id, copy, caption=f"{tag} {i}")
        copy.unlink(missing_ok=True)


async def test_a_second_queue_does_not_wait_for_the_first_one_to_finish(client, sample_video):
    """Two queues run at the same time — the whole point of having two.

    The first queue hands days of videos to Zernio. A queue added afterwards
    still places its first video within the hour, in the holes that delegation
    left, instead of starting after the last hour of it. What it must not do is
    land on top of anything the first queue owns.
    """
    await reset_mock()
    account_id = await make_account(client, ACCOUNT_1)
    first = await default_queue_id(client, account_id)
    await client.patch(f"/api/accounts/{account_id}",
                       json={"min_gap_minutes": 15, "max_per_day": None})
    await put_queue_schedule(client, first, name="Principal", windows=[], max_per_day=2,
                             interval_min_minutes=60, interval_max_minutes=60, jitter_seconds=0)
    await _fill(client, first, sample_video, 10, "primera")

    handed = await client.post(f"/api/schedules/{first}/delegate", json={})
    assert handed.status_code == 200, handed.text
    assert handed.json()["delegated"] == 10

    entry = await account_entry(client, account_id)
    booked = sorted(datetime.fromisoformat(v["scheduled_at"])
                    for v in entry["videos"] if v["status"] == "DELEGATED")
    now = datetime.now(timezone.utc)
    assert booked[-1] > now + timedelta(days=3), "la primera cola tiene que ocupar varios días"

    second = await add_queue(client, account_id, name="Segunda")
    await put_queue_schedule(client, second, name="Segunda", windows=[], max_per_day=2,
                             interval_min_minutes=60, interval_max_minutes=60,
                             jitter_seconds=0)
    await _fill(client, second, sample_video, 2, "segunda")

    entry = await account_entry(client, account_id)
    slot = next(q["schedule"]["next_slot_at"] for q in entry["queues"]
                if q["schedule"]["id"] == second)
    assert slot is not None, "la segunda cola no encontró ningún hueco"
    when = datetime.fromisoformat(slot)
    assert when < now + timedelta(hours=1), (
        f"la segunda cola espera a que termine la primera ({slot})")
    assert min(abs((when - b).total_seconds()) / 60 for b in booked) >= 15, \
        "se pisa con lo que la primera cola ya tiene reservado"

    # The preview agrees, and keeps its distance for every slot it shows.
    slots = (await client.get(f"/api/schedules/{second}/preview?count=6")).json()["slots"]
    for entry_slot in slots:
        moment = datetime.fromisoformat(entry_slot["utc"])
        assert min(abs((moment - b).total_seconds()) / 60 for b in booked) >= 15, entry_slot


async def test_the_preview_shows_the_hour_that_is_really_pinned(client, sample_video):
    """A queue's own next video must not count as an obstacle to itself.

    The head already holds the slot the simulation recomputes, so counting it
    would show a first hour one whole separation later than the real one.
    """
    await reset_mock()
    account_id = await make_account(client, ACCOUNT_1)
    only = await default_queue_id(client, account_id)
    await client.patch(f"/api/accounts/{account_id}",
                       json={"min_gap_minutes": 60, "max_per_day": None})
    await put_queue_schedule(client, only, windows=[], jitter_seconds=0, max_per_day=None,
                             interval_min_minutes=90, interval_max_minutes=90)
    await _fill(client, only, sample_video, 3, "prev")

    entry = await account_entry(client, account_id)
    pinned = datetime.fromisoformat(entry["queues"][0]["schedule"]["next_slot_at"])
    slots = (await client.get(f"/api/schedules/{only}/preview?count=3")).json()["slots"]
    first = datetime.fromisoformat(slots[0]["utc"])
    assert abs((first - pinned).total_seconds()) < 60, (slots[0]["local"], pinned)


async def test_a_stale_hour_is_recomputed_and_a_retry_is_not(client, sample_video):
    """What a previous version pinned is redone; a retry keeps its own plazo.

    ``replan_all`` runs once after a migration. It has to undo an hour the old
    rules chose — otherwise a rule change would not reach the videos already
    waiting — without touching a video that is backing off from a failure,
    because that hour is an embargo, not a plan.
    """
    await reset_mock()
    account_id, day, night = await _two_queues(client)
    for queue_id in (day, night):
        await put_queue_schedule(client, queue_id, windows=[], enabled=True, jitter_seconds=0,
                                 interval_min_minutes=30, interval_max_minutes=30)
    await _fill(client, day, sample_video, 1, "vieja")
    await _fill(client, night, sample_video, 1, "reintento")

    from app.db import session_scope
    from app.models import Schedule
    from app.services import queue as queue_svc, scheduling

    far = datetime.now(timezone.utc) + timedelta(days=14)
    with session_scope() as session:
        for queue_id, attempts in ((day, 0), (night, 2)):
            schedule = session.get(Schedule, queue_id)
            schedule.next_slot_at = far
            head = queue_svc.next_up(session, queue_id)
            head.scheduled_at = far
            head.attempts = attempts

    with session_scope() as session:
        assert scheduling.replan_all(session) == 2

    entry = await account_entry(client, account_id)
    slots = {q["schedule"]["id"]: datetime.fromisoformat(q["schedule"]["next_slot_at"])
             for q in entry["queues"]}
    assert slots[day] < datetime.now(timezone.utc) + timedelta(hours=1), \
        "la hora vieja se quedó clavada"
    assert slots[night] == far, "un reintento conserva su propio plazo"


async def test_the_queue_whose_hour_came_first_publishes(client, sample_video):
    """Two queues are overdue at once: the oldest slot wins, not the first one.

    Picking by position instead would let a busy first queue starve the rest.
    """
    await reset_mock()
    account_id, day, night = await _two_queues(client)
    for queue_id in (day, night):
        await put_queue_schedule(client, queue_id, windows=[], enabled=True, jitter_seconds=0,
                                 interval_min_minutes=1, interval_max_minutes=1)
    await _fill(client, day, sample_video, 1, "dia")
    await _fill(client, night, sample_video, 1, "noche")

    # «Noche» sits second in the list but its hour passed first.
    from app.db import session_scope
    from app.models import Schedule
    from app.services import queue as queue_svc

    earlier = datetime.now(timezone.utc) - timedelta(hours=2)
    with session_scope() as session:
        schedule = session.get(Schedule, night)
        schedule.next_slot_at = earlier
        queue_svc.next_up(session, night).scheduled_at = earlier

    from tests.helpers import tick
    await tick()

    entry = await account_entry(client, account_id)
    moving = [v for v in entry["videos"]
              if v["status"] in ("UPLOADING", "PROCESSING", "PUBLISHED")]
    assert len(moving) == 1, [v["status"] for v in entry["videos"]]
    assert moving[0]["queue_name"] == "Noche", moving[0]


async def test_only_one_queue_uploads_at_a_time(client, sample_video):
    """Both queues are due at once: the tick must publish exactly one video."""
    await reset_mock()
    account_id, day, night = await _two_queues(client)
    for queue_id in (day, night):
        await put_queue_schedule(
            client, queue_id, windows=[], enabled=True, min_gap_minutes=0, jitter_seconds=0,
            interval_min_minutes=1, interval_max_minutes=1,
        )
    await upload_to_queue(client, day, sample_video)
    # A different file: the same one may not sit in two queues of one account.
    await upload_to_queue(client, night, _clip(sample_video, "otra.mp4", b"\x00"))

    from tests.helpers import tick
    await tick()
    entry = await account_entry(client, account_id)
    moving = [v for v in entry["videos"] if v["status"] in ("UPLOADING", "PROCESSING",
                                                            "PUBLISHED")]
    assert len(moving) == 1, [v["status"] for v in entry["videos"]]


# --------------------------------------------------------------------------- #
# Deleting a queue
# --------------------------------------------------------------------------- #

async def test_the_last_queue_cannot_be_deleted(client):
    await reset_mock()
    account_id = await make_account(client)
    only = await default_queue_id(client, account_id)
    resp = await client.delete(f"/api/schedules/{only}")
    assert resp.status_code == 409
    assert "al menos una cola" in resp.json()["detail"]


async def test_deleting_a_queue_with_videos_needs_a_decision(client, sample_video):
    """Neither option given: nothing is deleted, and the message says why."""
    await reset_mock()
    account_id, day, night = await _two_queues(client)
    await upload_to_queue(client, night, sample_video)

    refused = await client.delete(f"/api/schedules/{night}")
    assert refused.status_code == 409
    assert "elige a qué cola moverlos" in refused.json()["detail"]
    assert len((await account_entry(client, account_id))["queues"]) == 2

    both = await client.delete(f"/api/schedules/{night}?move_to={day}&purge=true")
    assert both.status_code == 400


async def test_a_queue_can_hand_its_videos_to_another(client, sample_video):
    await reset_mock()
    account_id, day, night = await _two_queues(client)
    await upload_to_queue(client, night, sample_video)

    ok = await client.delete(f"/api/schedules/{night}?move_to={day}")
    assert ok.status_code == 200 and ok.json()["moved"] == 1

    entry = await account_entry(client, account_id)
    assert [q["schedule"]["name"] for q in entry["queues"]] == ["Día"]
    assert [v["queue_name"] for v in entry["videos"]] == ["Día"]
    assert entry["queues"][0]["stats"]["queued"] == 1


async def test_purging_a_queue_deletes_its_videos_and_its_history(client, sample_video):
    await reset_mock()
    account_id, day, night = await _two_queues(client)
    await put_queue_schedule(client, night, name="Noche", windows=[], enabled=True,
                             min_gap_minutes=0, jitter_seconds=0)
    published = await upload_to_queue(client, night, sample_video)
    await drain(account_id)
    await upload_to_queue(client, night, _clip(sample_video, "otra.mp4", b"\x01"))
    entry = await account_entry(client, account_id)
    # Uno publicado y otro esperando su hueco.
    assert {v["status"] for v in entry["videos"]} == {"PUBLISHED", "SCHEDULED"}
    on_disk = Path(next(v["file_path"] for v in entry["videos"] if v["status"] != "PUBLISHED"))

    gone = await client.delete(f"/api/schedules/{night}?purge=true")
    assert gone.status_code == 200
    assert gone.json()["removed"] == 2 and gone.json()["files_deleted"] == 0

    entry = await account_entry(client, account_id)
    assert [q["schedule"]["name"] for q in entry["queues"]] == ["Día"]
    assert entry["videos"] == [], "la cola se lleva sus vídeos y su historial"
    assert entry["stats"]["published"] == 0
    # Los archivos siguen en disco mientras no se pida lo contrario.
    assert on_disk.exists(), on_disk

    # …y el registro de actividad se conserva entero.
    events = (await client.get("/api/events")).json()["events"]
    kinds = [e["kind"] for e in events]
    assert "queue_deleted" in kinds and "published" in kinds
    assert any(published["original_name"] in e["message"] for e in events)


async def test_purging_never_takes_the_library_files(client, sample_video):
    """The file is shared with the other queues: it goes from the library page."""
    await reset_mock()
    account_id, day, night = await _two_queues(client)
    video = await upload_to_queue(client, night, sample_video)
    on_disk = Path(video["file_path"])
    assert on_disk.exists()

    gone = await client.delete(f"/api/schedules/{night}?purge=true&delete_files=true")
    assert gone.status_code == 200
    assert gone.json()["removed"] == 1 and gone.json()["files_deleted"] == 0
    assert on_disk.exists()
    assert (await client.get("/api/library")).json()["total"] == 1


async def test_a_queue_being_uploaded_cannot_be_deleted(client, sample_video):
    await reset_mock()
    account_id, day, night = await _two_queues(client)
    await put_queue_schedule(client, night, name="Noche", windows=[], enabled=True,
                             min_gap_minutes=0, jitter_seconds=0)
    await upload_to_queue(client, night, sample_video)
    await configure_mock(publish_delay_seconds=600)
    from tests.helpers import tick
    await tick()

    entry = await account_entry(client, account_id)
    assert entry["videos"][0]["status"] in ("UPLOADING", "PROCESSING")
    refused = await client.delete(f"/api/schedules/{night}?purge=true")
    assert refused.status_code == 409 and "subiéndose" in refused.json()["detail"]


async def test_a_video_left_without_a_queue_is_adopted_back(client, sample_video):
    """Only reachable by deleting a schedules row by hand in the DB browser."""
    await reset_mock()
    account_id, day, night = await _two_queues(client)
    await upload_to_queue(client, night, sample_video)

    from app.db import session_scope
    from app.models import Schedule, VideoItem

    with session_scope() as session:
        session.delete(session.get(Schedule, night))
    with session_scope() as session:
        assert session.query(VideoItem).one().schedule_id is None

    # Stop «Día» first: adoption happens whatever the queue's state, and
    # otherwise the same tick would publish the video whenever the test runs
    # inside the 10-22 window, leaving nothing queued to assert on.
    await client.patch(f"/api/schedules/{day}", json={"enabled": False})

    from tests.helpers import tick
    await tick()

    entry = await account_entry(client, account_id)
    assert [v["queue_name"] for v in entry["videos"]] == ["Día"]
    assert entry["queues"][0]["stats"]["queued"] == 1


async def test_an_empty_queue_is_deleted_outright(client):
    await reset_mock()
    account_id, day, night = await _two_queues(client)
    assert (await client.delete(f"/api/schedules/{night}")).status_code == 200
    entry = await account_entry(client, account_id)
    assert len(entry["queues"]) == 1 and entry["account"]["queue_count"] == 1


async def test_deleted_accounts_take_their_queues_along(client):
    await reset_mock()
    account_id, _, _ = await _two_queues(client)
    assert (await client.delete(f"/api/accounts/{account_id}")).status_code == 200
    from app.db import session_scope
    from app.models import Schedule

    with session_scope() as session:
        assert session.query(Schedule).count() == 0
