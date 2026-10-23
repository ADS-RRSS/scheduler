"""«Mezclar»: a random order for the waiting videos, as many times as wanted.

What matters is where that order goes afterwards: the queue releases its
videos in it, and handing the queue to Zernio keeps it too.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.db import session_scope
from app.models import VideoItem, VideoStatus
from app.services import queue
from tests.helpers import (
    account_entry,
    add_queue,
    default_queue_id,
    make_account,
    mock_state,
    put_queue_schedule,
    reset_mock,
    tick,
    upload_to_queue,
)


async def _fill(client, queue_id: int, video: Path, count: int, start: int = 0) -> list[int]:
    """``count`` distinct copies of the sample clip; their ids, in arrival order.

    The queue deduplicates by checksum, so every copy carries its own tail.
    """
    ids = []
    for i in range(start, start + count):
        copy = video.with_name(f"shuffle-{i}-{video.name}")
        copy.write_bytes(video.read_bytes() + bytes([i]))
        ids.append((await upload_to_queue(client, queue_id, copy, caption=f"Vídeo {i}"))["id"])
        copy.unlink(missing_ok=True)
    return ids


def _order(queue_id: int) -> list[int]:
    """The waiting videos of a queue, in the order they will go out."""
    with session_scope() as session:
        return [item.id for item in queue.pending(session, queue_id)]


async def _shuffle(client, queue_id: int) -> dict:
    resp = await client.post(f"/api/schedules/{queue_id}/shuffle")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _queue(client, video: Path, count: int) -> tuple[int, int, list[int]]:
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, interval_min_minutes=30,
                             interval_max_minutes=30, max_per_day=None)
    return account_id, queue_id, await _fill(client, queue_id, video, count)


def _scheduled(entry: dict) -> list[int]:
    return [v["id"] for v in entry["videos"] if v["status"] == "SCHEDULED"]


# --------------------------------------------------------------------------- #
# The order itself
# --------------------------------------------------------------------------- #

async def test_a_shuffle_deals_a_new_order_with_the_same_videos(client, sample_video):
    account_id, queue_id, arrived = await _queue(client, sample_video, 6)
    assert _order(queue_id) == arrived

    res = await _shuffle(client, queue_id)
    order = _order(queue_id)
    assert res["shuffled"] == 6 and res["held"] == 0
    assert res["order"] == order
    assert order != arrived
    assert sorted(order) == sorted(arrived), "ni se pierde ni se repite ninguno"

    # The new head holds the queue's next slot, and nobody else keeps one.
    entry = await account_entry(client, account_id)
    assert _scheduled(entry) == [order[0]]
    assert entry["queues"][0]["schedule"]["next_slot_at"] is not None
    assert any("mezclada" in e["message"] for e in (await client.get("/api/state")).json()["events"])


async def test_every_press_draws_another_order(client, sample_video):
    _, queue_id, arrived = await _queue(client, sample_video, 5)
    orders = [arrived]
    for _ in range(6):
        await _shuffle(client, queue_id)
        orders.append(_order(queue_id))
    # A press never leaves the queue as it was, and it is no fixed swap
    # between two orders either.
    assert all(a != b for a, b in zip(orders, orders[1:]))
    assert len({tuple(o) for o in orders}) > 2


async def test_a_video_that_arrives_after_a_shuffle_joins_the_end(client, sample_video):
    _, queue_id, _ = await _queue(client, sample_video, 4)
    await _shuffle(client, queue_id)
    shuffled = _order(queue_id)

    late = await _fill(client, queue_id, sample_video, 2, start=10)
    assert _order(queue_id) == shuffled + late

    # The next shuffle deals them in with the rest.
    await _shuffle(client, queue_id)
    assert sorted(_order(queue_id)) == sorted(shuffled + late)


async def test_arrival_order_undoes_the_shuffle(client, sample_video):
    account_id, queue_id, arrived = await _queue(client, sample_video, 5)
    await _shuffle(client, queue_id)
    assert _order(queue_id) != arrived

    resp = await client.post(f"/api/schedules/{queue_id}/unshuffle")
    assert resp.status_code == 200, resp.text
    assert resp.json()["reset"] == 5
    assert _order(queue_id) == arrived
    assert _scheduled(await account_entry(client, account_id)) == [arrived[0]]


async def test_it_takes_two_waiting_videos(client, sample_video):
    _, queue_id, _ = await _queue(client, sample_video, 1)
    resp = await client.post(f"/api/schedules/{queue_id}/shuffle")
    assert resp.status_code == 400
    assert "dos vídeos" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# What is already on its way stays in front
# --------------------------------------------------------------------------- #

async def test_a_video_sent_with_ahora_stays_first(client, sample_video):
    _, queue_id, arrived = await _queue(client, sample_video, 5)
    forced = arrived[3]
    resp = await client.post(f"/api/videos/{forced}/publish-now")
    assert resp.status_code == 200, resp.text

    for _ in range(4):
        res = await _shuffle(client, queue_id)
        assert res["held"] == 1 and res["shuffled"] == 4
        assert _order(queue_id)[0] == forced
    with session_scope() as session:
        assert session.get(VideoItem, forced).force_publish


async def test_a_retry_keeps_the_front_and_its_embargo(client, sample_video):
    """A head waiting to retry must not end up SCHEDULED in the middle of the queue."""
    _, queue_id, arrived = await _queue(client, sample_video, 5)
    embargo = datetime.now(timezone.utc) + timedelta(hours=3)
    with session_scope() as session:
        head = session.get(VideoItem, arrived[0])
        head.status, head.attempts, head.scheduled_at = VideoStatus.SCHEDULED, 1, embargo

    for _ in range(3):
        await _shuffle(client, queue_id)
        assert _order(queue_id)[0] == arrived[0]
    with session_scope() as session:
        head = session.get(VideoItem, arrived[0])
        assert head.status == VideoStatus.SCHEDULED
        assert head.scheduled_at == embargo
        others = [session.get(VideoItem, i) for i in arrived[1:]]
        assert all(v.status == VideoStatus.QUEUED for v in others)


# --------------------------------------------------------------------------- #
# Where the order goes
# --------------------------------------------------------------------------- #

async def test_zernio_gets_the_videos_in_the_shuffled_order(client, sample_video):
    account_id, queue_id, arrived = await _queue(client, sample_video, 5)
    await _shuffle(client, queue_id)
    order = _order(queue_id)
    assert order != arrived

    plan = (await client.get(f"/api/schedules/{queue_id}/delegation")).json()
    assert [p["video_id"] for p in plan["posts"]] == order

    resp = await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    assert resp.status_code == 200, resp.text
    for _ in range(10):
        if not (await account_entry(client, account_id))["stats"]["handover_pending"]:
            break
        await tick()

    with session_scope() as session:
        post_of = {item.id: item.zernio_post_id for item in session.query(VideoItem)}
    posts = (await mock_state())["posts"]
    assert len(posts) == 5

    def hour(video_id: int) -> datetime:
        stamp = posts[post_of[video_id]]["scheduled_for"]
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))

    # Zernio publishes them in the shuffled order, one interval apart...
    hours = [hour(video_id) for video_id in order]
    assert hours == sorted(hours) and len(set(hours)) == 5
    # ...and they went up in that order too.
    video_of = {post_id: video_id for video_id, post_id in post_of.items()}
    assert [video_of[post_id] for post_id in posts] == order


async def test_recovering_from_zernio_brings_the_shuffled_order_back(client, sample_video):
    account_id, queue_id, _ = await _queue(client, sample_video, 4)
    await _shuffle(client, queue_id)
    order = _order(queue_id)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    for _ in range(10):
        if not (await account_entry(client, account_id))["stats"]["handover_pending"]:
            break
        await tick()

    resp = await client.post(f"/api/schedules/{queue_id}/delegation/cancel")
    assert resp.status_code == 200, resp.text
    assert _order(queue_id) == order


async def test_a_moved_video_leaves_its_turn_behind(client, sample_video):
    """A turn from one queue's shuffle must not cut into another queue's."""
    await reset_mock()
    account_id = await make_account(client)
    queue_a = await default_queue_id(client, account_id)
    queue_b = await add_queue(client, account_id, "Noche")
    await _fill(client, queue_a, sample_video, 3)
    await _fill(client, queue_b, sample_video, 3, start=10)
    await _shuffle(client, queue_a)
    await _shuffle(client, queue_b)
    in_b = _order(queue_b)

    # A's first turn: kept, it would be dealt first in B as well.
    moved = _order(queue_a)[0]
    resp = await client.patch(f"/api/videos/{moved}", json={"schedule_id": queue_b})
    assert resp.status_code == 200, resp.text
    assert _order(queue_b) == in_b + [moved]


# --------------------------------------------------------------------------- #
# Moving videos by hand
# --------------------------------------------------------------------------- #

async def _reorder(client, queue_id: int, ids: list[int], **where):
    return await client.post(f"/api/schedules/{queue_id}/reorder", json={"ids": ids, **where})


async def test_a_video_moved_to_a_turn_takes_it_and_the_head_follows(client, sample_video):
    account_id, queue_id, a = await _queue(client, sample_video, 5)
    resp = await _reorder(client, queue_id, [a[3]], position=1)
    assert resp.status_code == 200, resp.text
    assert resp.json()["first"] == 1 and "turno 1 de 5" in resp.json()["message"]
    assert _order(queue_id) == [a[3], a[0], a[1], a[2], a[4]]
    assert _scheduled(await account_entry(client, account_id)) == [a[3]]

    # Past the end means last; a block keeps the order it had in the queue.
    await _reorder(client, queue_id, [a[0], a[3]], position=99)
    assert _order(queue_id) == [a[1], a[2], a[4], a[3], a[0]]
    await _reorder(client, queue_id, [a[4], a[1]], position=2)
    assert _order(queue_id) == [a[2], a[1], a[4], a[3], a[0]]


async def test_a_drop_lands_before_or_after_another_video(client, sample_video):
    _, queue_id, a = await _queue(client, sample_video, 5)
    await _reorder(client, queue_id, [a[0]], after=a[2])
    assert _order(queue_id) == [a[1], a[2], a[0], a[3], a[4]]
    await _reorder(client, queue_id, [a[4]], before=a[1])
    assert _order(queue_id) == [a[4], a[1], a[2], a[0], a[3]]

    assert (await _reorder(client, queue_id, [a[0]], before=a[0])).status_code == 400
    assert (await _reorder(client, queue_id, [a[0]])).status_code == 400
    assert (await _reorder(client, queue_id, [999999], position=1)).status_code == 400


async def test_a_manual_order_keeps_ahora_in_front_and_newcomers_at_the_end(client, sample_video):
    _, queue_id, a = await _queue(client, sample_video, 4)
    await client.post(f"/api/videos/{a[2]}/publish-now")
    await _reorder(client, queue_id, [a[3]], position=1)
    assert _order(queue_id) == [a[2], a[3], a[0], a[1]]
    # The video on its way does not move, and nothing goes ahead of it.
    assert (await _reorder(client, queue_id, [a[2]], position=4)).status_code == 400
    await _reorder(client, queue_id, [a[1]], before=a[2])
    assert _order(queue_id)[:2] == [a[2], a[1]]

    (late,) = await _fill(client, queue_id, sample_video, 1, start=50)
    assert _order(queue_id)[-1] == late
    # «Orden de llegada» undoes a manual order too.
    await client.post(f"/api/schedules/{queue_id}/unshuffle")
    assert _order(queue_id) == [a[2], a[0], a[1], a[3], late]
