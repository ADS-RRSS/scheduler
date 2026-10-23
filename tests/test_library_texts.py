"""Reviewing and editing the library's texts in bulk."""
from __future__ import annotations

from pathlib import Path

from tests.helpers import default_queue_id, enqueue, make_account, upload_to_library
from tests.test_autocaption import ai_on, fake_whisper  # noqa: F401 - fixtures


async def _clip(client, sample_video: Path, tmp_root: Path, n: int, caption: str | None,
                name: str | None = None):
    clip = tmp_root / (name or f"reel_{n:03d}_habitos_tema-{n}.mp4")
    clip.write_bytes(sample_video.read_bytes() + bytes([n]))
    return await upload_to_library(client, clip, caption=caption)


async def test_the_list_carries_the_review_and_filters_by_it(client, sample_video, tmp_root):
    good = await _clip(client, sample_video, tmp_root, 1,
                       "Por qué la constancia gana a la motivación\n¿Cuál te cuesta más?")
    bad = await _clip(client, sample_video, tmp_root, 2, "Sabías que madrugar cambia todo?")
    rows = (await client.get("/api/library/items", params={"folder": "habitos"})).json()["items"]
    review = {r["id"]: r["review"] for r in rows}
    assert review[good["id"]]["warnings"] == 0 and review[good["id"]]["errors"] == 0
    assert review[bad["id"]]["warnings"] >= 2
    codes = {i["code"] for i in review[bad["id"]]["issues"]}
    assert {"hook_stock", "open_marks"} <= codes

    only = (await client.get("/api/library/items",
                             params={"folder": "habitos", "issues": "true"})).json()
    assert only["total"] == 1 and [r["id"] for r in only["items"]] == [bad["id"]]

    # Fixed in the editor: the review follows the change.
    fixed = {"title": "¿Madrugar cambia el día?",
             "description": "Por qué madrugar cambia el día\n¿Lo has probado?"}
    resp = await client.patch(f"/api/library/{bad['id']}", json={"tiktok": fixed, "youtube": fixed})
    assert resp.status_code == 200
    only = (await client.get("/api/library/items",
                             params={"folder": "habitos", "issues": "true"})).json()
    assert only["total"] == 0


async def test_hashtags_are_added_and_removed_across_a_folder(client, sample_video, tmp_root):
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    items = [await _clip(client, sample_video, tmp_root, n, f"Texto {n}\n¿Y tú?") for n in (3, 4)]
    for item in items:
        await client.patch(f"/api/library/{item['id']}", json={
            "tiktok": {"tags": ["#habitos", "#fyp"]}, "youtube": {"tags": ["#habitos"]}})
    queued = (await enqueue(client, queue_id, items[0]["id"])).json()["added"][0]

    resp = await client.post("/api/library/tags", json={
        "folder": "habitos", "platforms": ["tiktok"], "add": ["Productividad"],
        "remove": ["#fyp"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] == 2 and body["followers"] == 1
    for item in items:
        texts = (await client.get(f"/api/library/{item['id']}")).json()["texts"]
        assert texts["tiktok"]["tags"] == ["#productividad", "#habitos"]
        assert texts["youtube"]["tags"] == ["#habitos"], "sólo la plataforma elegida"
    video = (await client.get(f"/api/videos/{queued['id']}")).json()
    assert video["auto_hashtags"] == ["#productividad", "#habitos"]

    again = (await client.post("/api/library/tags", json={
        "ids": [items[0]["id"]], "add": ["#productividad"], "position": "end"})).json()
    assert again["changed"] == 1  # YouTube did not have it yet
    assert (await client.post("/api/library/tags", json={"folder": "habitos"})).status_code == 400


async def test_bulk_hashtags_leave_texts_being_written_alone(client, sample_video, tmp_root,
                                                             ai_on, fake_whisper):
    writing = await _clip(client, sample_video, tmp_root, 5, None)
    assert writing["caption_status"] == "pending"
    body = (await client.post("/api/library/tags", json={
        "ids": [writing["id"]], "add": ["#habitos"]})).json()
    assert body["changed"] == 0 and body["writing"] == [writing["original_name"]]
    assert "se están escribiendo" in body["message"]


async def test_health_says_how_much_disk_is_left(client):
    disk = (await client.get("/api/health")).json()["disk"]
    assert disk["free"] > 0 and disk["total"] >= disk["free"] and disk["low"] in (True, False)
