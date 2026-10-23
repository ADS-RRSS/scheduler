"""A text asked for by hand does not wait behind a folder being written in bulk."""
from __future__ import annotations

from pathlib import Path

from tests.helpers import default_queue_id, enqueue, make_account, upload_to_library
from tests.test_autocaption import ai_on, fake_whisper, run_captioner  # noqa: F401 - fixtures


async def _clip(client, sample_video: Path, tmp_root: Path, n: int, caption: str | None = None):
    clip = tmp_root / f"clip_{n}.mp4"
    clip.write_bytes(sample_video.read_bytes() + bytes([n]))
    return await upload_to_library(client, clip, caption=caption)


async def _status(client, library_id: int) -> str:
    return (await client.get(f"/api/library/{library_id}")).json()["caption_status"]


async def test_rewrite_on_one_video_goes_ahead_of_the_bulk(client, sample_video, tmp_root,
                                                            ai_on, fake_whisper):
    fake_whisper()
    bulk = [await _clip(client, sample_video, tmp_root, n) for n in (1, 2, 3)]
    assert all(item["caption_status"] == "pending" for item in bulk)
    # «Reescribir» on the last one: asked for by hand, so it jumps the line.
    resp = await client.post(f"/api/library/{bulk[-1]['id']}/rewrite")
    assert resp.status_code == 200, resp.text

    assert await run_captioner() is True
    assert await _status(client, bulk[-1]["id"]) == "done"
    assert [await _status(client, item["id"]) for item in bulk[:-1]] == ["pending", "pending"]

    # A bulk rewrite is bulk: it keeps the order it was asked in.
    resp = await client.post("/api/library/bulk", json={"action": "rewrite",
                                                        "ids": [bulk[-1]["id"]]})
    assert resp.status_code == 200
    assert await run_captioner() is True
    assert await _status(client, bulk[0]["id"]) == "done"
    assert await _status(client, bulk[-1]["id"]) == "pending"


async def test_regenerate_in_a_queue_goes_ahead_of_the_library(client, sample_video, tmp_root,
                                                               ai_on, fake_whisper):
    fake_whisper()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    own = await _clip(client, sample_video, tmp_root, 9, caption="Texto escrito a mano")
    resp = await enqueue(client, queue_id, own["id"])
    video = resp.json()["added"][0]
    waiting = [await _clip(client, sample_video, tmp_root, n) for n in (4, 5)]

    resp = await client.post(f"/api/videos/{video['id']}/caption/regenerate")
    assert resp.status_code == 200, resp.text
    assert await run_captioner() is True
    after = (await client.get(f"/api/videos/{video['id']}")).json()
    assert after["caption_status"] == "done" and after["caption_source"] != "biblioteca"
    assert [await _status(client, item["id"]) for item in waiting] == ["pending", "pending"]


async def test_the_state_says_how_long_the_texts_will_take(client, sample_video, tmp_root,
                                                           ai_on, fake_whisper):
    from app.scheduler.captioner import captioner

    fake_whisper()
    captioner.durations.clear()
    for n in (6, 7, 8):
        await _clip(client, sample_video, tmp_root, n)
    queue_before = (await client.get("/api/state")).json()["captions"]
    assert queue_before["waiting"] == 3 and queue_before["eta_seconds"] is None

    assert await run_captioner() is True
    queue_after = (await client.get("/api/state")).json()["captions"]
    each = captioner.seconds_per_text()
    assert each is not None and queue_after["seconds_each"] == each
    assert queue_after["waiting"] == 2
    assert queue_after["eta_seconds"] == round(2 * each)
