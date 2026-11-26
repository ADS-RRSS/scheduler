"""YouTube: the account's category at upload time, and the channel's videos and
metadata that Tunnel's YouTube optimizer reads and fixes through Zernio."""
from __future__ import annotations

from pathlib import Path

import httpx

from tests.helpers import (
    MOCK_BASE,
    default_queue_id,
    drain,
    enqueue,
    mock_state,
    reset_mock,
    upload_to_library,
)

YOUTUBE = "acc_youtube_1"


async def _youtube_account(client) -> int:
    resp = await client.post("/api/accounts", json={"api_key": "zernio-test-key",
                                                    "account_ids": [YOUTUBE]})
    assert resp.status_code == 200, resp.text
    return resp.json()["added"][0]["id"]


async def _seed_external(videos: list[dict]) -> None:
    async with httpx.AsyncClient() as c:
        await c.post(f"{MOCK_BASE}/_mock/youtube_external", json=videos)


async def test_account_category_goes_out_with_every_youtube_video(client, sample_video):
    await reset_mock()
    account_id = await _youtube_account(client)
    resp = await client.patch(f"/api/accounts/{account_id}", json={"youtube_category_id": "27"})
    assert resp.status_code == 200 and resp.json()["youtube_category_id"] == "27"

    item = await upload_to_library(client, sample_video, name="clase.mp4")
    queue_id = await default_queue_id(client, account_id)
    assert (await enqueue(client, queue_id, item["id"])).json()["added"]
    video = (await client.get("/api/state")).json()["accounts"][0]["videos"][0]
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)

    (post,) = (await mock_state())["posts"].values()
    assert post["platform_data"]["categoryId"] == "27"


async def test_category_can_be_cleared_and_must_be_a_number(client):
    await reset_mock()
    account_id = await _youtube_account(client)
    await client.patch(f"/api/accounts/{account_id}", json={"youtube_category_id": "27"})
    assert (await client.patch(f"/api/accounts/{account_id}",
                               json={"youtube_category_id": "Educación"})).status_code == 422
    resp = await client.patch(f"/api/accounts/{account_id}", json={"youtube_category_id": ""})
    assert resp.json()["youtube_category_id"] is None


async def test_without_category_youtube_keeps_its_default(client, sample_video):
    await reset_mock()
    account_id = await _youtube_account(client)
    item = await upload_to_library(client, sample_video, name="otra.mp4")
    assert (await enqueue(client, await default_queue_id(client, account_id), item["id"])).json()["added"]
    video = (await client.get("/api/state")).json()["accounts"][0]["videos"][0]
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)
    (post,) = (await mock_state())["posts"].values()
    assert "categoryId" not in post["platform_data"]


async def test_channel_videos_join_both_lists_oldest_first_and_honour_since(client):
    await reset_mock()
    account_id = await _youtube_account(client)
    await _seed_external([
        {"video_id": "LARGO00002", "title": "Examen · Preguntas 16 a 30",
         "published_at": "2026-11-26T15:00:00Z"},
        {"video_id": "LARGO00001", "title": "Examen · Preguntas 1 a 15",
         "published_at": "2026-11-26T14:00:00Z"},
        {"video_id": "OTRACUENTA", "title": "De otro canal", "account_id": "acc_otro",
         "published_at": "2026-11-26T16:00:00Z"},
    ])
    resp = await client.get(f"/api/youtube/{account_id}/videos")
    assert resp.status_code == 200, resp.text
    videos = resp.json()["videos"]
    assert [v["video_id"] for v in videos] == ["LARGO00001", "LARGO00002"]
    assert videos[0]["origin"] == "external"
    assert videos[0]["title"] == "Examen · Preguntas 1 a 15"

    later = await client.get(f"/api/youtube/{account_id}/videos",
                             params={"since": "2026-11-26T14:00:00Z"})
    assert [v["video_id"] for v in later.json()["videos"]] == ["LARGO00002"]


async def test_tiktok_account_is_refused(client):
    await reset_mock()
    resp = await client.post("/api/accounts", json={"api_key": "zernio-test-key",
                                                    "account_ids": ["acc_tiktok_1"]})
    tiktok = resp.json()["added"][0]["id"]
    assert (await client.get(f"/api/youtube/{tiktok}/videos")).status_code == 400


async def test_metadata_dry_run_sends_nothing(client, tmp_root: Path):
    await reset_mock()
    account_id = await _youtube_account(client)
    thumb = tmp_root / "miniatura.jpg"
    thumb.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    resp = await client.post(f"/api/youtube/{account_id}/videos/abc123/metadata", json={
        "category_id": "27", "default_language": "es-ES", "default_audio_language": "es-ES",
        "playlist_id": "PLx", "thumbnail_path": str(thumb), "dry_run": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["would_send"] == {"categoryId": "27", "defaultLanguage": "es-ES",
                                  "defaultAudioLanguage": "es-ES", "playlistId": "PLx"}
    state = await mock_state()
    assert state["metadata_updates"] == [] and state["media"] == {}


async def test_metadata_is_sent_in_direct_mode_with_the_thumbnail_uploaded(client, tmp_root: Path):
    await reset_mock()
    account_id = await _youtube_account(client)
    thumb = tmp_root / "miniatura2.jpg"
    thumb.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    resp = await client.post(f"/api/youtube/{account_id}/videos/abc123/metadata", json={
        "category_id": "27", "default_language": "es-ES", "playlist_id": "PLx",
        "thumbnail_path": str(thumb)})
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["updated"]) == {"categoryId", "defaultLanguage", "playlistId",
                                           "thumbnailUrl"}
    (sent,) = (await mock_state())["metadata_updates"]
    assert sent["ref"] == "_" and sent["videoId"] == "abc123" and sent["accountId"] == YOUTUBE
    assert sent["thumbnailUrl"].startswith("https://media.mock.invalid/")
    events = (await client.get("/api/events?kind=youtube_metadata_updated")).json()["events"]
    assert len(events) == 1 and "Educación" in events[0]["message"]


async def test_metadata_refuses_nothing_to_change_and_bad_thumbnails(client, tmp_root: Path):
    await reset_mock()
    account_id = await _youtube_account(client)
    url = f"/api/youtube/{account_id}/videos/abc123/metadata"
    assert (await client.post(url, json={})).status_code == 400
    assert (await client.post(url, json={"thumbnail_path": str(tmp_root / "no.jpg")})).status_code == 400
    assert (await client.post(url, json={"thumbnail_path": str(tmp_root / "x.gifv")})).status_code == 400
