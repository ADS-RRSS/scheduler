"""Handing a whole queue to Zernio, hours included.

The point of delegating is that the computer can be turned off afterwards, so
these tests check the two halves of that promise: every video reaches Zernio
with the hour the local scheduler would have used, and nothing here publishes
it twice in the meantime.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from tests.helpers import (
    MOCK_BASE,
    ZERNIO_KEY,
    account_entry,
    configure_mock,
    default_queue_id,
    make_account,
    mock_state,
    put_queue_schedule,
    reset_mock,
    tick,
    upload_to_queue,
)


async def _fill(client, queue_id: int, video: Path, count: int,
                start: int = 0) -> list[dict]:
    """``count`` copies of the sample clip, each with different bytes.

    The queue deduplicates by checksum, so every copy carries its own tail.
    """
    added = []
    for i in range(start, start + count):
        copy = video.with_name(f"delegate-{i}-{video.name}")
        copy.write_bytes(video.read_bytes() + bytes([i]))
        added.append(await upload_to_queue(client, queue_id, copy, caption=f"Vídeo {i}"))
        copy.unlink(missing_ok=True)
    return added


def _videos(entry: dict, status: str | None = None) -> list[dict]:
    return [v for v in entry["videos"] if status is None or v["status"] == status]


async def _tick_until_uploaded(client, account_id: int, max_ticks: int = 10) -> None:
    """The worker sends one video per tick; run it until the batch is up."""
    for _ in range(max_ticks):
        entry = await account_entry(client, account_id)
        if not entry["stats"]["handover_pending"]:
            return
        await tick()


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

async def test_preview_spaces_every_video_and_changes_nothing(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, interval_min_minutes=30,
                             interval_max_minutes=30, max_per_day=None)
    await _fill(client, queue_id, sample_video, 4)

    plan = (await client.get(f"/api/schedules/{queue_id}/delegation")).json()
    assert plan["count"] == 4
    assert plan["left_behind"] == 0
    # Consecutive, in queue order, exactly one interval apart.
    times = [datetime.fromisoformat(p["utc"]) for p in plan["posts"]]
    assert times == sorted(times)
    assert all(g == 30 for g in [p["gap_minutes"] for p in plan["posts"][1:]])
    assert plan["gap_min"] == plan["gap_max"] == 30

    # A preview is a simulation: the queue is untouched.
    entry = await account_entry(client, account_id)
    assert entry["stats"]["delegated"] == 0
    assert len(_videos(entry, "QUEUED")) + len(_videos(entry, "SCHEDULED")) == 4


async def test_daily_cap_spreads_the_plan_over_several_days(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, interval_min_minutes=30,
                             interval_max_minutes=30, max_per_day=2)
    await _fill(client, queue_id, sample_video, 5)

    plan = (await client.get(f"/api/schedules/{queue_id}/delegation")).json()
    assert plan["count"] == 5
    per_day: dict[str, int] = {}
    for post in plan["posts"]:
        per_day[post["date"]] = per_day.get(post["date"], 0) + 1
    assert max(per_day.values()) <= 2
    assert plan["days"] == len(per_day) >= 3


# --------------------------------------------------------------------------- #
# Handing over
# --------------------------------------------------------------------------- #

async def test_delegating_sends_every_video_with_its_scheduled_hour(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, interval_min_minutes=45,
                             interval_max_minutes=45, max_per_day=None)
    await _fill(client, queue_id, sample_video, 3)
    plan = (await client.get(f"/api/schedules/{queue_id}/delegation")).json()

    resp = await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["delegated"] == 3

    # Out of the local rotation at once, bytes still to go.
    entry = await account_entry(client, account_id)
    assert entry["stats"] == {**entry["stats"], "delegated": 3, "handover_pending": 3,
                              "queued": 0}

    await _tick_until_uploaded(client, account_id)
    entry = await account_entry(client, account_id)
    assert entry["stats"]["handover_pending"] == 0
    assert entry["stats"]["delegated"] == 3
    assert all(v["handed_over"] for v in _videos(entry, "DELEGATED"))

    posts = list((await mock_state())["posts"].values())
    assert len(posts) == 3
    assert all(p["publish_now"] is False for p in posts), "no se publica al entregar"
    assert all(p["timezone"] == "Europe/Madrid" for p in posts)
    # The hours Zernio got are the ones the preview promised. A slot that had
    # already passed by the time its turn to upload came (the first one, which
    # the planner puts at "now") is nudged a few minutes forward instead of
    # being sent stale, which would make Zernio publish it on the spot.
    sent = sorted(datetime.fromisoformat(p["scheduled_for"].replace("Z", "+00:00"))
                  for p in posts)
    planned = [datetime.fromisoformat(p["utc"]) for p in plan["posts"]]
    assert all(when > datetime.now(timezone.utc) for when in sent)
    for got, wanted in zip(sent, planned):
        assert -90 <= (got - wanted).total_seconds() <= 5 * 60
    gaps = [round((b - a).total_seconds() / 60) for a, b in zip(sent, sent[1:])]
    assert gaps[-1] == 45, "la separación entre vídeos es la del intervalo de la cola"


async def test_a_delegated_queue_is_not_published_locally(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    # A queue whose slot is due right now: without delegation it would publish.
    await put_queue_schedule(client, queue_id, interval_min_minutes=1,
                             interval_max_minutes=1, max_per_day=None)
    await _fill(client, queue_id, sample_video, 2)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})

    await _tick_until_uploaded(client, account_id)
    for _ in range(3):
        await tick()

    entry = await account_entry(client, account_id)
    assert len(_videos(entry, "PUBLISHED")) == 0
    assert len(_videos(entry, "DELEGATED")) == 2
    # One post per video and not one more: nothing was sent twice.
    assert len((await mock_state())["posts"]) == 2


async def test_zernio_publishes_it_and_the_queue_finds_out(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None)
    await _fill(client, queue_id, sample_video, 1)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    await _tick_until_uploaded(client, account_id)

    # The hour arrives: Zernio publishes and the next poll sees it.
    await configure_mock(release_scheduled=True)
    from app.db import session_scope
    from app.models import VideoItem

    with session_scope() as session:
        for item in session.query(VideoItem).all():
            item.next_status_check_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await tick()

    entry = await account_entry(client, account_id)
    published = _videos(entry, "PUBLISHED")
    assert len(published) == 1
    assert published[0]["post_url"].startswith("https://www.tiktok.com/@")
    # The file stays in the library: another queue may still publish it.
    assert Path(published[0]["file_path"]).exists()
    assert "library" in Path(published[0]["file_path"]).parts


async def _catch_up(client, account_id: int, *, hours_ago: float) -> list[dict]:
    """Zernio published the batch ``hours_ago``; the app only finds out now.

    Reproduces the real sequence: the videos are handed over, the computer is
    closed, and a much later poll discovers them already published.
    """
    from app.db import session_scope
    from app.models import VideoItem

    now = datetime.now(timezone.utc)
    with session_scope() as session:
        for item in session.query(VideoItem).all():
            item.scheduled_at = now - timedelta(hours=hours_ago)
            item.next_status_check_at = now - timedelta(minutes=1)
    await configure_mock(release_scheduled=True)
    await tick()
    return _videos(await account_entry(client, account_id), "PUBLISHED")


async def test_a_delegated_post_is_dated_by_its_hour_not_by_the_catch_up(client, sample_video):
    """Finding out late must not move the publication to the moment we looked.

    The analytics read `published_at` to say which hour of the day works, so a
    batch Zernio published overnight cannot all be dated at breakfast.
    """
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None)
    await _fill(client, queue_id, sample_video, 1)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    await _tick_until_uploaded(client, account_id)

    published = await _catch_up(client, account_id, hours_ago=5)
    assert len(published) == 1
    when = datetime.fromisoformat(published[0]["published_at"])
    scheduled = datetime.fromisoformat(published[0]["scheduled_at"])
    assert when == scheduled, "se quedó con la hora en que la app se dio cuenta"
    assert when < datetime.now(timezone.utc) - timedelta(hours=4)


async def test_a_whole_batch_caught_up_at_once_keeps_its_hours_apart(client, sample_video):
    """Three videos discovered in one tick must not share one instant."""
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None, interval_min_minutes=60,
                             interval_max_minutes=60, jitter_seconds=0)
    await _fill(client, queue_id, sample_video, 3)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    await _tick_until_uploaded(client, account_id)

    # Spread the three an hour apart in the past, then catch up on all of them.
    from app.db import session_scope
    from app.models import VideoItem

    now = datetime.now(timezone.utc)
    with session_scope() as session:
        for i, item in enumerate(session.query(VideoItem).order_by(VideoItem.id).all()):
            item.scheduled_at = now - timedelta(hours=6 - i)
            item.next_status_check_at = now - timedelta(minutes=1)
    await configure_mock(release_scheduled=True)
    for _ in range(4):
        await tick()

    published = _videos(await account_entry(client, account_id), "PUBLISHED")
    assert len(published) == 3, [v["status"] for v in published]
    hours = sorted(datetime.fromisoformat(v["published_at"]) for v in published)
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(hours, hours[1:])]
    assert gaps == [60, 60], gaps


async def test_zernio_s_own_publication_time_wins(client, sample_video):
    """When Zernio reports `publishedAt`, nothing is inferred."""
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None)
    await _fill(client, queue_id, sample_video, 1)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    await _tick_until_uploaded(client, account_id)

    real = (datetime.now(timezone.utc) - timedelta(hours=2, minutes=13)).replace(microsecond=0)
    await configure_mock(published_at=real.isoformat().replace("+00:00", "Z"))

    published = await _catch_up(client, account_id, hours_ago=5)
    assert len(published) == 1
    assert datetime.fromisoformat(published[0]["published_at"]) == real


async def test_reopening_the_app_days_later_still_finds_it_published(client, sample_video):
    """The deadline is not a verdict on its own: Zernio is asked first.

    The usual case: the laptop stays closed for three days, Zernio publishes
    on time, and the first poll after reopening is already past 48 h.
    """
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None)
    await _fill(client, queue_id, sample_video, 1)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    await _tick_until_uploaded(client, account_id)

    published = await _catch_up(client, account_id, hours_ago=72)
    assert len(published) == 1, "se dio por perdido sin preguntar a Zernio"
    assert published[0]["last_error"] is None


async def test_a_post_zernio_still_holds_past_the_deadline_fails(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None)
    await _fill(client, queue_id, sample_video, 1)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    await _tick_until_uploaded(client, account_id)

    from app.db import session_scope
    from app.models import VideoItem

    now = datetime.now(timezone.utc)
    with session_scope() as session:
        for item in session.query(VideoItem).all():
            item.scheduled_at = now - timedelta(hours=72)
            item.next_status_check_at = now - timedelta(minutes=1)
    await tick()  # Zernio was not told to release it: still scheduled there

    failed = _videos(await account_entry(client, account_id), "FAILED")
    assert len(failed) == 1
    assert failed[0]["last_error"].startswith("Zernio no confirmó la publicación")


# --------------------------------------------------------------------------- #
# Taking it back
# --------------------------------------------------------------------------- #

async def test_one_video_can_be_recovered_from_zernio(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None)
    await _fill(client, queue_id, sample_video, 2)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    await _tick_until_uploaded(client, account_id)

    entry = await account_entry(client, account_id)
    victim = _videos(entry, "DELEGATED")[0]
    post_id = victim["zernio_post_id"]

    resp = await client.post(f"/api/videos/{victim['id']}/undelegate")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] in ("QUEUED", "SCHEDULED")

    state = await mock_state()
    assert post_id in state["cancelled"], "hay que cancelarlo en Zernio, no sólo aquí"
    assert post_id not in state["posts"]

    entry = await account_entry(client, account_id)
    assert entry["stats"]["delegated"] == 1
    assert entry["stats"]["queued"] == 1


async def test_the_whole_batch_can_be_recovered(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None)
    await _fill(client, queue_id, sample_video, 3)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    await _tick_until_uploaded(client, account_id)

    resp = await client.post(f"/api/schedules/{queue_id}/delegation/cancel")
    assert resp.status_code == 200, resp.text
    assert resp.json()["recovered"] == 3
    assert len((await mock_state())["posts"]) == 0

    entry = await account_entry(client, account_id)
    assert entry["stats"]["delegated"] == 0
    assert entry["stats"]["queued"] == 3
    assert entry["queues"][0]["schedule"]["next_slot_at"] is not None


async def test_a_post_zernio_already_published_is_marked_and_the_batch_goes_on(
        client, sample_video):
    """«Published posts cannot be deleted»: its hour came before the poll saw it."""
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None)
    await _fill(client, queue_id, sample_video, 3)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    await _tick_until_uploaded(client, account_id)

    entry = await account_entry(client, account_id)
    first = sorted(_videos(entry, "DELEGATED"), key=lambda v: v["scheduled_at"])[0]
    await configure_mock(published_ids=[first["zernio_post_id"]])

    resp = await client.post(f"/api/schedules/{queue_id}/delegation/cancel")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["recovered"] == 2 and body["published"] == 1 and not body["problem"]
    assert "ya estaban publicados" in body["message"]

    entry = await account_entry(client, account_id)
    assert entry["stats"]["delegated"] == 0
    assert entry["stats"]["queued"] == 2
    done = next(v for v in entry["videos"] if v["id"] == first["id"])
    assert done["status"] == "PUBLISHED"
    assert done["post_url"], "el enlace se pide a Zernio"
    # Dated by its slot (here still ahead, so now), never by when the recovery found out.
    assert (datetime.fromisoformat(done["published_at"])
            <= datetime.fromisoformat(first["scheduled_at"]))


async def test_recovering_one_video_zernio_already_published_marks_it(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None)
    await _fill(client, queue_id, sample_video, 1)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    await _tick_until_uploaded(client, account_id)

    video = _videos(await account_entry(client, account_id), "DELEGATED")[0]
    await configure_mock(published_ids=[video["zernio_post_id"]])

    resp = await client.post(f"/api/videos/{video['id']}/undelegate")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "PUBLISHED"


async def _fill_series(client, queue_id: int, video: Path, series: str, count: int,
                       start: int) -> list[dict]:
    """``count`` clips named after ``series``, so the library files them there."""
    added = []
    for i in range(start, start + count):
        copy = video.with_name(f"reel_{i:02d}_{series}_tema-{i}.mp4")
        copy.write_bytes(video.read_bytes() + bytes([i]))
        added.append(await upload_to_queue(client, queue_id, copy, caption=f"Vídeo {i}"))
        copy.unlink(missing_ok=True)
    return added


async def test_only_one_folder_can_be_sent_and_the_rest_waits_behind_it(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None)
    others = await _fill_series(client, queue_id, sample_video, "serie-a", 2, start=0)
    chosen = await _fill_series(client, queue_id, sample_video, "serie-b", 2, start=10)

    plan = (await client.get(f"/api/schedules/{queue_id}/delegation",
                             params={"folder": "serie-b"})).json()
    assert plan["count"] == 2 and plan["folder"] == "serie-b"
    assert {p["video_id"] for p in plan["posts"]} == {v["id"] for v in chosen}
    assert any("Sólo se envía la carpeta" in w for w in plan["warnings"])

    resp = await client.post(f"/api/schedules/{queue_id}/delegate", json={"folder": "serie-b"})
    assert resp.status_code == 200, resp.text
    entry = await account_entry(client, account_id)
    delegated = _videos(entry, "DELEGATED")
    assert {v["id"] for v in delegated} == {v["id"] for v in chosen}
    last = max(datetime.fromisoformat(v["scheduled_at"]) for v in delegated)
    local = [v for v in entry["videos"] if v["id"] in {o["id"] for o in others}]
    assert all(v["status"] in ("QUEUED", "SCHEDULED") for v in local)
    head = entry["queues"][0]["schedule"]["next_slot_at"]
    assert datetime.fromisoformat(head) > last

    empty = await client.post(f"/api/schedules/{queue_id}/delegate", json={"folder": "serie-b"})
    assert empty.status_code == 400


async def test_a_folder_or_a_selection_can_be_recovered_from_zernio(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None)
    a = await _fill_series(client, queue_id, sample_video, "serie-a", 2, start=0)
    b = await _fill_series(client, queue_id, sample_video, "serie-b", 3, start=10)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    await _tick_until_uploaded(client, account_id)

    resp = await client.post(f"/api/schedules/{queue_id}/delegation/cancel",
                             json={"folder": "serie-a"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["recovered"] == 2
    entry = await account_entry(client, account_id)
    assert {v["id"] for v in _videos(entry, "DELEGATED")} == {v["id"] for v in b}

    picked = [b[0]["id"], b[2]["id"], a[0]["id"]]  # a[0] is no longer in Zernio
    resp = await client.post(f"/api/schedules/{queue_id}/delegation/cancel",
                             json={"video_ids": picked})
    assert resp.json()["recovered"] == 2
    entry = await account_entry(client, account_id)
    assert [v["id"] for v in _videos(entry, "DELEGATED")] == [b[1]["id"]]
    assert len((await mock_state())["posts"]) == 1


async def test_a_label_sends_and_recovers_every_folder_that_carries_it(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None)
    a = await _fill_series(client, queue_id, sample_video, "serie-a", 2, start=0)
    b = await _fill_series(client, queue_id, sample_video, "serie-b", 1, start=10)
    c = await _fill_series(client, queue_id, sample_video, "serie-c", 2, start=20)
    for folder in ("serie-a", "serie-c"):
        await client.post("/api/library/folders/labels", json={"folder": folder, "labels": ["lista"]})
    state = (await client.get("/api/state")).json()["library"]
    assert state["folder_labels"] == {"serie-a": ["lista"], "serie-c": ["lista"]}

    plan = (await client.get(f"/api/schedules/{queue_id}/delegation",
                             params={"label": "Lista"})).json()
    labelled = {v["id"] for v in a + c}
    assert plan["count"] == 4 and plan["folders"] == ["serie-a", "serie-c"] and plan["folder"] is None
    assert {p["video_id"] for p in plan["posts"]} == labelled
    assert any("etiqueta «lista»" in w for w in plan["warnings"])

    resp = await client.post(f"/api/schedules/{queue_id}/delegate", json={"label": "lista"})
    assert resp.status_code == 200, resp.text
    entry = await account_entry(client, account_id)
    assert {v["id"] for v in _videos(entry, "DELEGATED")} == labelled
    assert (await client.post(f"/api/schedules/{queue_id}/delegate",
                              json={"label": "nada"})).status_code == 400

    await _tick_until_uploaded(client, account_id)
    resp = await client.post(f"/api/schedules/{queue_id}/delegation/cancel", json={"label": "lista"})
    assert resp.json()["recovered"] == 4
    entry = await account_entry(client, account_id)
    assert not _videos(entry, "DELEGATED")
    assert len(entry["videos"]) == len(a + b + c)


async def test_folders_and_labels_combine_into_one_scope(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None)
    a = await _fill_series(client, queue_id, sample_video, "serie-a", 1, start=0)
    b = await _fill_series(client, queue_id, sample_video, "serie-b", 1, start=10)
    c = await _fill_series(client, queue_id, sample_video, "serie-c", 1, start=20)
    for folder, labels in (("serie-a", ["serie", "terminada"]), ("serie-b", ["serie"]),
                           ("serie-c", ["terminada"])):
        await client.post("/api/library/folders/labels", json={"folder": folder, "labels": labels})
    url = f"/api/schedules/{queue_id}/delegation"

    async def planned(**params) -> set[int]:
        return {p["video_id"] for p in (await client.get(url, params=params)).json()["posts"]}

    ids = lambda *groups: {v["id"] for g in groups for v in g}  # noqa: E731
    # Every folder, narrowed by both labels: only the one that has both.
    assert await planned(labels=["serie", "terminada"]) == ids(a)
    assert await planned(labels=["serie", "terminada"], match="any") == ids(a, b, c)
    # Two folders ticked, narrowed by one label.
    assert await planned(folders=["serie-b", "serie-c"], labels=["serie"]) == ids(b)
    assert await planned(folders=["serie-a", "serie-c"]) == ids(a, c)
    plan = (await client.get(url, params={"labels": ["serie", "terminada"]})).json()
    assert any("etiquetas «serie» y «terminada»" in w for w in plan["warnings"])

    resp = await client.post(f"/api/schedules/{queue_id}/delegate",
                             json={"folders": ["serie-b", "serie-c"], "labels": ["terminada"]})
    assert resp.status_code == 200, resp.text
    entry = await account_entry(client, account_id)
    assert {v["id"] for v in _videos(entry, "DELEGATED")} == ids(c)


async def test_waiting_videos_can_be_removed_in_bulk_and_the_rest_stays(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None)
    videos = await _fill(client, queue_id, sample_video, 4)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={"limit": 1})
    parked = videos[0]["id"]

    resp = await client.post(f"/api/schedules/{queue_id}/videos/remove",
                             json={"video_ids": [parked, videos[1]["id"], videos[2]["id"]]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["removed"] == 2 and resp.json()["skipped"] == 1

    entry = await account_entry(client, account_id)
    assert {v["id"]: v["status"] for v in entry["videos"]} == {
        parked: "DELEGATED", videos[3]["id"]: "SCHEDULED"}
    # The library keeps the files: other queues may need them.
    assert all(Path(v["file_path"]).exists() for v in videos)


async def test_deleting_a_delegated_video_cancels_it_in_zernio(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None)
    await _fill(client, queue_id, sample_video, 1)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    await _tick_until_uploaded(client, account_id)

    entry = await account_entry(client, account_id)
    video = _videos(entry, "DELEGATED")[0]
    resp = await client.delete(f"/api/videos/{video['id']}")
    assert resp.status_code == 200, resp.text

    state = await mock_state()
    assert video["zernio_post_id"] in state["cancelled"]
    entry = await account_entry(client, account_id)
    assert entry["videos"] == []


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #

async def test_an_empty_queue_cannot_be_delegated(client):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    resp = await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    assert resp.status_code == 400
    assert "pendientes" in resp.json()["detail"]


async def test_a_deactivated_account_cannot_delegate(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await _fill(client, queue_id, sample_video, 1)
    await client.patch(f"/api/accounts/{account_id}", json={"is_active": False})

    resp = await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    assert resp.status_code == 409
    assert "desactivada" in resp.json()["detail"]


async def test_a_video_queued_after_a_delegation_goes_after_it(client, sample_video):
    """The local scheduler must not book an hour inside the delegated batch."""
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, interval_min_minutes=60,
                             interval_max_minutes=60, max_per_day=None)
    await _fill(client, queue_id, sample_video, 3)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})

    entry = await account_entry(client, account_id)
    last = max(datetime.fromisoformat(v["scheduled_at"])
               for v in _videos(entry, "DELEGATED"))

    await _fill(client, queue_id, sample_video, 1, start=90)
    entry = await account_entry(client, account_id)
    fresh = _videos(entry, "SCHEDULED") + _videos(entry, "QUEUED")
    assert len(fresh) == 1
    assert datetime.fromisoformat(fresh[0]["scheduled_at"]) >= last + timedelta(minutes=59)


async def test_a_queue_with_videos_in_zernio_cannot_be_purged(client, sample_video):
    """Deleting the rows would not stop Zernio: the queue has to be emptied first."""
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    other = (await client.post(f"/api/accounts/{account_id}/schedules",
                               json={"name": "Otra"})).json()["schedule"]["id"]
    await put_queue_schedule(client, queue_id, max_per_day=None)
    await _fill(client, queue_id, sample_video, 2)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    await _tick_until_uploaded(client, account_id)

    refused = await client.delete(f"/api/schedules/{queue_id}?purge=true&delete_files=true")
    assert refused.status_code == 409
    assert "entregados a Zernio" in refused.json()["detail"]

    # Moving them is fine: they keep their hour and their post in Zernio.
    moved = await client.delete(f"/api/schedules/{queue_id}?move_to={other}")
    assert moved.status_code == 200, moved.text
    entry = await account_entry(client, account_id)
    assert entry["stats"]["delegated"] == 2
    assert all(v["schedule_id"] == other for v in _videos(entry, "DELEGATED"))


async def _delegated_queue(client, video: Path, count: int) -> tuple[int, int, list[dict]]:
    """An account with a spare queue and ``count`` videos parked in Zernio."""
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await client.post(f"/api/accounts/{account_id}/schedules", json={"name": "Otra"})
    await put_queue_schedule(client, queue_id, max_per_day=None)
    await _fill(client, queue_id, video, count)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    await _tick_until_uploaded(client, account_id)
    parked = _videos(await account_entry(client, account_id), "DELEGATED")
    assert len(parked) == count
    return account_id, queue_id, parked


async def test_a_queue_emptied_in_zernio_by_hand_can_be_deleted_anyway(client, sample_video):
    """The user wiped Zernio's queue: the rows point at nothing, so let them go."""
    await reset_mock()
    account_id, queue_id, parked = await _delegated_queue(client, sample_video, 3)
    async with httpx.AsyncClient() as zernio:
        gone = await zernio.delete(f"{MOCK_BASE}/v1/posts/{parked[0]['zernio_post_id']}",
                                   headers={"Authorization": f"Bearer {ZERNIO_KEY}"})
        assert gone.status_code == 200

    resp = await client.delete(f"/api/schedules/{queue_id}?purge=true&force=true")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["removed"] == 3
    # The one already deleted in Zernio answers 404, which counts as cancelled.
    assert body["cancelled_in_zernio"] == 3
    assert body["left_in_zernio"] == 0
    assert (await mock_state())["posts"] == {}

    entry = await account_entry(client, account_id)
    assert [q["schedule"]["name"] for q in entry["queues"]] == ["Otra"]
    assert entry["videos"] == []


async def test_a_forced_deletion_reports_what_zernio_would_not_cancel(client, sample_video):
    await reset_mock()
    account_id, queue_id, _ = await _delegated_queue(client, sample_video, 2)
    await configure_mock(fail_delete=True)

    resp = await client.delete(f"/api/schedules/{queue_id}?purge=true&force=true")
    assert resp.status_code == 200, resp.text
    assert resp.json()["left_in_zernio"] == 2
    assert len((await mock_state())["posts"]) == 2

    entry = await account_entry(client, account_id)
    assert entry["videos"] == []
    events = (await client.get("/api/events", params={"kind": "delegation_orphaned"})).json()
    assert "siguen en Zernio" in events["events"][0]["message"]


async def test_force_alone_does_not_cancel_anything(client, sample_video):
    """Without purge the deletion is refused, so Zernio must not be touched."""
    await reset_mock()
    _, queue_id, _ = await _delegated_queue(client, sample_video, 2)

    resp = await client.delete(f"/api/schedules/{queue_id}?force=true")
    assert resp.status_code == 409
    state = await mock_state()
    assert len(state["posts"]) == 2
    assert state["cancelled"] == []


def _during_next_upload(monkeypatch, action) -> None:
    """Run ``action`` while the next video's bytes are on their way to Zernio."""
    from app.services.zernio import ZernioClient

    original = ZernioClient.publish

    async def publish(self, *args, **kwargs):
        monkeypatch.setattr(ZernioClient, "publish", original)
        await action()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(ZernioClient, "publish", publish)


async def test_a_video_recovered_mid_upload_does_not_stay_in_zernio(
    client, sample_video, monkeypatch
):
    """Recovering it re-queues it locally; the post the upload creates must go."""
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None)
    await _fill(client, queue_id, sample_video, 1)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})

    async def recover():
        resp = await client.post(f"/api/schedules/{queue_id}/delegation/cancel")
        assert resp.json()["recovered"] == 1

    _during_next_upload(monkeypatch, recover)
    await tick()

    state = await mock_state()
    assert state["posts"] == {}, "publicaría en Zernio y otra vez desde la cola local"
    assert len(state["cancelled"]) == 1
    video = _videos(await account_entry(client, account_id))[0]
    assert video["status"] in ("QUEUED", "SCHEDULED")
    assert video["zernio_post_id"] is None


async def test_a_queue_deleted_mid_upload_leaves_nothing_in_zernio(
    client, sample_video, monkeypatch
):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await client.post(f"/api/accounts/{account_id}/schedules", json={"name": "Otra"})
    await put_queue_schedule(client, queue_id, max_per_day=None)
    await _fill(client, queue_id, sample_video, 2)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})

    async def delete_queue():
        resp = await client.delete(f"/api/schedules/{queue_id}?purge=true&force=true")
        assert resp.status_code == 200, resp.text

    _during_next_upload(monkeypatch, delete_queue)
    await tick()

    state = await mock_state()
    assert state["posts"] == {}
    assert len(state["cancelled"]) == 1
    entry = await account_entry(client, account_id)
    assert entry["videos"] == []
    events = (await client.get("/api/events", params={"kind": "delegated_orphan"})).json()
    assert "se ha cancelado" in events["events"][0]["message"]


async def test_publish_now_refuses_a_video_parked_in_zernio(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None)
    await _fill(client, queue_id, sample_video, 1)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})

    entry = await account_entry(client, account_id)
    video = _videos(entry, "DELEGATED")[0]
    resp = await client.post(f"/api/videos/{video['id']}/publish-now")
    assert resp.status_code == 409
    assert "Recupéralo" in resp.json()["detail"]


async def test_recovering_many_goes_in_rounds_of_ten_with_a_pause(monkeypatch):
    """Zernio rate-limits a burst of deletes, so a big recovery paces itself."""
    from types import SimpleNamespace

    from app.config import get_settings
    from app.services import delegation, events, queue, scheduling
    from app.services.zernio import ZernioError

    items = [SimpleNamespace(id=i, folder=None) for i in range(25)]
    log: list[str] = []
    limited = {13}

    async def fake_cancel(session, account, item, settings, **kwargs):
        if item.id in limited:
            limited.discard(item.id)
            raise ZernioError("zernio_rate_limit", "Too many requests", retryable=True,
                              http_status=429, retry_after=2)
        log.append(f"cancel:{item.id}")
        return True

    async def fake_sleep(seconds):
        log.append(f"sleep:{seconds}")

    monkeypatch.setattr(delegation, "cancel", fake_cancel)
    monkeypatch.setattr(delegation.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(delegation, "in_folder", lambda found, folder: found)
    monkeypatch.setattr(delegation, "pending_count", lambda session, account_id: 0)
    monkeypatch.setattr(queue, "delegated", lambda *args: items)
    monkeypatch.setattr(scheduling, "plan_next_slot", lambda *args: None)
    monkeypatch.setattr(events, "record", lambda *args, **kwargs: None)

    account = SimpleNamespace(id=1, schedules=[])
    session = SimpleNamespace(commit=lambda: log.append("commit"))
    result = await delegation.cancel_batch(session, account, get_settings())

    assert result["recovered"] == 25 and not result["problem"]
    # Each recovery is committed before the next request or pause: the write
    # lock is never held while waiting on Zernio.
    cancels = [i for i, entry in enumerate(log) if entry.startswith("cancel")]
    assert all(log[i + 1] == "commit" for i in cancels)
    assert log.count("commit") == 25
    log[:] = [entry for entry in log if entry != "commit"]
    sleeps = [i for i, entry in enumerate(log) if entry.startswith("sleep")]
    assert [log[i] for i in sleeps] == ["sleep:5.0", "sleep:5.0", "sleep:5.0"]
    # Pause before the 11th and the 21st video; the third pause is the 429 retry of #13.
    assert log[sleeps[0] + 1] == "cancel:10"
    assert log[sleeps[1] - 1] == "cancel:12" and log[sleeps[1] + 1] == "cancel:13"
    assert log[sleeps[2] + 1] == "cancel:20"
