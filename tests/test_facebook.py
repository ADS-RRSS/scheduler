"""Facebook Pages through Zernio: a Reel or a feed video, its own texts, no cover."""
from __future__ import annotations

from datetime import datetime, timezone

from app.services import publisher
from app.services import textcheck as tc
from app.services.zernio import PostOptions, ZernioClient
from tests.helpers import (
    default_queue_id,
    drain,
    enqueue,
    mock_state,
    put_schedule,
    reset_mock,
    upload_to_library,
)

FACEBOOK = "acc_facebook_1"


async def _connect(client) -> int:
    resp = await client.post("/api/accounts", json={
        "api_key": "zernio-test-key", "account_ids": [FACEBOOK]})
    assert resp.status_code == 200, resp.text
    (added,) = resp.json()["added"]
    assert added["platform"] == "facebook"
    return added["id"]


def test_a_vertical_minute_is_a_reel_anything_else_goes_to_the_feed():
    assert publisher.facebook_reel({"duration_sec": 4, "width": 1080, "height": 1920})
    assert publisher.facebook_reel({"duration_sec": 60, "width": 720, "height": 1280})
    assert not publisher.facebook_reel({"duration_sec": 150, "width": 1080, "height": 1920})
    assert not publisher.facebook_reel({"duration_sec": 30, "width": 1920, "height": 1080})
    assert not publisher.facebook_reel({"duration_sec": 2, "width": 1080, "height": 1920})
    assert not publisher.facebook_reel(None)


def test_the_post_body_carries_no_tiktok_settings():
    client = ZernioClient("k", "https://zernio.invalid/v1")
    options = PostOptions(caption="Texto #historia", title="La Transición", first_comment="¿Y tú?",
                          tags=("historia",), is_aigc=True, facebook_reel=True)
    body = client.build_post("acc", "https://media/x.mp4", options, platform="facebook",
                             thumbnail_url="https://media/x.jpg")
    (target,) = body["platforms"]
    assert target == {"platform": "facebook", "accountId": "acc", "platformSpecificData": {
        "contentType": "reel", "title": "La Transición", "firstComment": "¿Y tú?"}}
    assert body["mediaItems"] == [{"type": "video", "url": "https://media/x.mp4"}]
    assert not any(k.endswith("Settings") for k in body) and "tags" not in body
    assert body["publishNow"] is True

    feed = client.build_post("acc", "https://media/x.mp4", PostOptions(caption="Texto", title="T"),
                             scheduled_for=datetime(2026, 12, 1, 9, tzinfo=timezone.utc),
                             timezone_name="Europe/Madrid", platform="facebook")
    assert feed["platforms"][0]["platformSpecificData"] == {}      # no title outside a Reel
    assert feed["scheduledFor"] == "2026-12-01T09:00:00Z" and feed["publishNow"] is False


async def test_a_page_publishes_a_reel_with_its_own_texts_and_no_cover(client, sample_video):
    await reset_mock()
    page = await _connect(client)
    await put_schedule(client, page)
    item = await upload_to_library(client, sample_video)
    patched = await client.patch(f"/api/library/{item['id']}", json={
        "facebook": {"title": "La Transición en un minuto", "description": "Cómo se votó la Constitución",
                     "tags": ["#historia"], "first_comment": "¿Lo sabías?"}})
    assert patched.status_code == 200, patched.text
    (video,) = (await enqueue(client, await default_queue_id(client, page), item["id"])).json()["added"]
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(page)
    (post,) = (await mock_state())["posts"].values()
    assert post["platform"] == "facebook"
    assert post["platform_data"]["contentType"] == "reel"        # 4 s and vertical
    assert post["platform_data"]["title"] == "La Transición en un minuto"
    assert post["first_comment"] is None and post["platform_data"]["firstComment"] == "¿Lo sabías?"
    assert post["content"].startswith("Cómo se votó la Constitución")
    assert post["thumbnail_key"] is None and post["settings"] == {}


def test_facebook_has_its_own_limits():
    many = tc.Texts(title="", description="Un texto\n¿Y tú?", tags=[f"#tema{i}" for i in range(8)])
    report = tc.check(many, "facebook")
    assert "facebook_tags_many" in {i.code for i in report.issues}
    assert "title_missing" not in {i.code for i in report.issues}   # the title is a Reel's, optional
    assert report.as_dict()["stats"]["caption_max"] == tc.FACEBOOK_TEXT_MAX
    assert "facebook_tags_many" not in {i.code for i in tc.check(many, "tiktok").issues}
