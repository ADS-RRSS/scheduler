"""Publishing through Zernio: connecting accounts and the whole publish path.

The fake Zernio in tools/mock_zernio.py enforces the real contract: bearer
auth, presign -> PUT with a fixed Content-Length and matching Content-Type ->
create post with both consent flags -> status polling until the TikTok URL
appears. Nothing on our side is stubbed.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from tests.helpers import (
    MOCK_BASE,
    ACCOUNT_1,
    ACCOUNT_2,
    ZERNIO_KEY,
    account_entry,
    configure_mock,
    default_queue_id,
    drain,
    first_video,
    make_account,
    mock_state,
    put_queue_schedule,
    put_schedule,
    reset_mock,
    tick,
    upload_sample,
)


# --------------------------------------------------------------------------- #
# Connecting
# --------------------------------------------------------------------------- #

async def test_key_lists_tiktok_and_youtube_accounts_and_marks_connected_ones(client):
    await reset_mock()
    resp = await client.post("/api/zernio/accounts", json={"api_key": ZERNIO_KEY})
    assert resp.status_code == 200, resp.text
    accounts = resp.json()["accounts"]
    # TikTok and YouTube; the Instagram account of the same key is left out.
    assert [a["id"] for a in accounts] == [ACCOUNT_1, ACCOUNT_2, "acc_youtube_1"]
    assert [a["platform"] for a in accounts] == ["tiktok", "tiktok", "youtube"]
    assert all(a["already_connected"] is False for a in accounts)

    await make_account(client, ACCOUNT_1)
    accounts = (await client.post("/api/zernio/accounts", json={"api_key": ZERNIO_KEY})).json()["accounts"]
    assert {a["id"]: a["already_connected"] for a in accounts} == {
        ACCOUNT_1: True, ACCOUNT_2: False, "acc_youtube_1": False}


async def test_wrong_key_is_reported_clearly(client):
    await reset_mock()
    resp = await client.post("/api/zernio/accounts", json={"api_key": "not-the-key"})
    assert resp.status_code == 400
    assert "API key" in resp.json()["detail"]


async def test_new_account_defaults_and_hides_the_key(client):
    await reset_mock()
    account_id = await make_account(client, ACCOUNT_1, label="Principal")
    state = (await client.get("/api/state")).json()
    entry = state["accounts"][0]
    assert entry["account"]["id"] == account_id
    assert entry["account"]["username"] == "relay_user"
    assert entry["account"]["display_name"] == "Principal"
    assert entry["account"]["zernio_account_id"] == ACCOUNT_1
    assert entry["account"]["has_key"] is True and entry["account"]["is_active"] is True
    assert entry["queues"][0]["schedule"]["enabled"] is False
    assert ZERNIO_KEY not in json.dumps(state), "la API key nunca debe salir por /api/state"


async def test_several_accounts_can_be_connected_at_once(client):
    await reset_mock()
    resp = await client.post("/api/accounts", json={
        "api_key": ZERNIO_KEY, "account_ids": [ACCOUNT_1, ACCOUNT_2, "acc_unknown"],
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [a["zernio_account_id"] for a in body["added"]] == [ACCOUNT_1, ACCOUNT_2]
    assert body["skipped"] == [{"id": "acc_unknown", "reason": "no aparece en tu Zernio"}]
    assert len((await client.get("/api/state")).json()["accounts"]) == 2


async def test_connecting_the_same_account_twice_is_refused(client):
    await reset_mock()
    await make_account(client, ACCOUNT_1)
    resp = await client.post("/api/accounts", json={"api_key": ZERNIO_KEY, "account_ids": [ACCOUNT_1]})
    assert resp.status_code == 409
    assert "ya está conectada" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# Publish path
# --------------------------------------------------------------------------- #

async def test_publish_end_to_end(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    video = await upload_sample(client, account_id, sample_video, caption="Relay [hashtags]")
    assert video["status"] in ("QUEUED", "SCHEDULED")
    assert video["resolution"] == "1080x1920" and video["has_audio"] is True

    publish = await client.post(f"/api/videos/{video['id']}/publish-now")
    assert publish.status_code == 200
    assert "Zernio" in publish.json()["message"]

    await drain(account_id)

    item = await first_video(client)
    assert item["status"] == "PUBLISHED", item["last_error"]
    assert item["post_url"].startswith("https://www.tiktok.com/@relay_user/video/")
    # Nothing moves: the file is the library's.
    assert "library" in Path(item["file_path"]).parts and Path(item["file_path"]).exists()

    mock = await mock_state()
    (post,) = mock["posts"].values()
    assert post["account_id"] == ACCOUNT_1
    assert post["content"] == "Relay"
    assert post["settings"]["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert post["settings"]["content_preview_confirmed"] is True
    assert post["settings"]["express_consent_given"] is True
    assert "draft" not in post["settings"], "los borradores ya no existen"
    assert post["settings"]["allow_comment"] is True
    (media,) = mock["media"].values()
    assert media["uploaded"] and media["received"] == sample_video.stat().st_size


async def test_success_without_url_is_confirmed_after_the_grace_period(client, sample_video):
    """TikTok exposes the video id minutes later; we must not wait forever."""
    await reset_mock()
    await configure_mock(url_polls=10_000)
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    video = await upload_sample(client, account_id, sample_video)
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await tick()

    item = await first_video(client)
    assert item["status"] == "PROCESSING", "sin URL todavía: sigue en proceso"

    from app.db import session_scope
    from app.models import VideoItem

    with session_scope() as session:
        row = session.get(VideoItem, item["id"])
        row.upload_started_at = datetime.now(timezone.utc) - timedelta(minutes=6)
    await tick()
    item = await first_video(client)
    assert item["status"] == "PUBLISHED" and item["post_url"] is None


async def test_every_video_goes_out_public_and_direct(client, sample_video):
    """Neither drafts nor a privacy level survive: there is one way to publish."""
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)

    # Fields the old UI used to send are simply ignored, not rejected.
    await put_queue_schedule(client, queue_id, to_drafts=True, privacy_level="SELF_ONLY",
                             sound_boost_enabled=True)
    schedule = (await account_entry(client, account_id))["queues"][0]["schedule"]
    assert "to_drafts" not in schedule and "privacy_level" not in schedule
    assert "sound_boost_enabled" not in schedule

    video = await upload_sample(client, account_id, sample_video)
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)

    (post,) = (await mock_state())["posts"].values()
    assert post["settings"]["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert "draft" not in post["settings"]
    assert (await first_video(client))["status"] == "PUBLISHED"


async def test_commercial_disclosure_and_ai_label_are_forwarded(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    await put_schedule(client, account_id, brand_organic_toggle=True, is_aigc=True,
                       disable_comment=True)
    video = await upload_sample(client, account_id, sample_video)
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)
    (post,) = (await mock_state())["posts"].values()
    assert post["settings"]["commercialContentType"] == "brand_organic"
    assert post["settings"]["video_made_with_ai"] is True
    assert post["settings"]["allow_comment"] is False


async def test_invalid_key_fails_without_retrying(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    video = await upload_sample(client, account_id, sample_video)
    await configure_mock(key="rotated-key")  # key revoked after connecting
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)

    item = await first_video(client)
    assert item["status"] == "FAILED"
    assert item["attempts"] == 1, "un 401 no se arregla reintentando"
    assert "API key" in item["last_error"]
    assert Path(item["file_path"]).exists(), "un fallo no mueve el archivo de la biblioteca"


async def test_rate_limit_and_server_errors_are_retried(client, sample_video):
    for fail in (429, 500):
        await reset_mock()
        await configure_mock(fail=fail)
        from app.db import engine, init_db
        from app.models import Base
        Base.metadata.drop_all(engine)
        init_db()
        account_id = await make_account(client)
        await put_schedule(client, account_id)
        video = await upload_sample(client, account_id, sample_video)
        await client.post(f"/api/videos/{video['id']}/publish-now")
        await tick()
        item = await first_video(client)
        assert item["status"] == "SCHEDULED", (fail, item)
        assert item["attempts"] == 1 and item["scheduled_at"] is not None


async def test_plan_limit_is_fatal_and_explained(client, sample_video):
    await reset_mock()
    await configure_mock(fail=402)
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    video = await upload_sample(client, account_id, sample_video)
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)
    item = await first_video(client)
    assert item["status"] == "FAILED" and "plan" in item["last_error"].lower()


async def test_rejection_after_accept_marks_failed(client, sample_video):
    await reset_mock()
    await configure_mock(fail="failed")
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    video = await upload_sample(client, account_id, sample_video)
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)
    item = await first_video(client)
    assert item["status"] == "FAILED" and "rejected" in item["last_error"]


async def test_scheduled_publish_restarts_the_cadence(client, sample_video):
    """The normal (non-forced) path: the slot is due, Zernio publishes, and the
    next slot is computed from that publication."""
    await reset_mock()
    account_id = await make_account(client)
    await put_schedule(client, account_id, interval_min_minutes=60, interval_max_minutes=60)
    await upload_sample(client, account_id, sample_video)
    await drain(account_id)

    entry = (await client.get("/api/state")).json()["accounts"][0]
    assert entry["videos"][0]["status"] == "PUBLISHED"
    assert entry["queues"][0]["schedule"]["last_post_at"] is not None
    assert entry["stats"]["queued"] == 0


async def test_inactive_account_is_left_alone(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    video = await upload_sample(client, account_id, sample_video)
    assert (await client.patch(f"/api/accounts/{account_id}", json={"is_active": False})).status_code == 200

    assert (await client.post(f"/api/videos/{video['id']}/publish-now")).status_code == 409
    await tick()
    await tick()
    assert (await first_video(client))["status"] in ("QUEUED", "SCHEDULED")
    assert (await mock_state())["posts"] == {}


async def test_video_file_is_served_for_preview(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    video = await upload_sample(client, account_id, sample_video)
    resp = await client.get(f"/api/videos/{video['id']}/file", headers={"Range": "bytes=0-99"})
    assert resp.status_code in (200, 206)
    assert resp.headers["content-type"].startswith("video/")


async def test_caption_can_be_edited_before_posting(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    video = await upload_sample(client, account_id, sample_video)
    resp = await client.patch(f"/api/videos/{video['id']}", json={"caption": "Nuevo texto"})
    assert resp.status_code == 200 and resp.json()["caption"] == "Nuevo texto"


# --------------------------------------------------------------------------- #
# Pure client behaviour
# --------------------------------------------------------------------------- #

def test_tiktok_settings_mapping():
    from app.services.zernio import PostOptions, ZernioClient

    settings = ZernioClient.tiktok_settings(PostOptions(
        caption="x", disable_stitch=True, brand_content_toggle=True,
        brand_organic_toggle=True, is_aigc=True,
    ))
    # Public is not a choice: it is the only thing the Business app lane posts.
    assert settings["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert settings["allow_stitch"] is False and settings["allow_duet"] is True
    # Branded content wins when both are ticked (it is the stricter label).
    assert settings["commercialContentType"] == "brand_content"
    assert settings["video_made_with_ai"] is True
    assert "draft" not in settings


def test_post_parsing_handles_every_shape():
    from app.services.zernio import ZernioClient

    parse = ZernioClient.parse_post
    ok = parse({"post": {"_id": "p1", "status": "published", "platforms": [
        {"platform": "tiktok", "status": "success", "platformPostUrl": "https://t/1"}]}})
    assert ok.published and ok.post_url == "https://t/1" and ok.post_id == "p1"

    pending = parse({"post": {"_id": "p2", "status": "publishing", "platforms": [
        {"platform": "tiktok", "status": "pending"}]}})
    assert pending.status == "processing" and not pending.published

    failed = parse({"post": {"_id": "p3", "status": "partial", "platforms": [
        {"platform": "tiktok", "status": "failed", "error": "nope"}]}})
    assert failed.failed and failed.message == "nope"

    bare = parse({"_id": "p4", "status": "published", "platforms": []})
    assert bare.published and bare.post_url is None


def test_analytics_parsing_reads_the_ids_zernio_really_sends():
    """`GET /analytics` names a post `postId`/`latePostId`, never `_id`.

    Reading the wrong key cost nothing loudly: every entry was skipped, the
    sync reported "0 vídeos" and the metrics table stayed empty for days. The
    payload below is the documented shape, so a rename is caught here and not
    in production.
    """
    from app.services.zernio import ZernioClient

    body = {
        "overview": {"totalPosts": 2},
        "posts": [
            {
                "postId": "768621995457151312",      # TikTok's own video id
                "latePostId": "6aaaf03e752fd8f8f102ef05",  # the post Zernio created
                "platform": "tiktok",
                "status": "published",
                "syncStatus": "synced",
                "publishedAt": "2026-09-16T19:41:04Z",
                "analytics": {
                    "views": 1200, "likes": 90, "comments": 7, "shares": 4,
                    "profileViews": 15, "completionRate": 0.42, "engagementRate": 8.42,
                    "lastUpdated": "2026-09-17T06:00:00Z",
                },
            },
            # Synced from TikTok rather than published here: no Zernio id.
            {"postId": "999", "latePostId": None, "syncStatus": "pending",
             "analytics": {"views": 0}},
        ],
        "hasAnalyticsAccess": True,
    }
    first, second = ZernioClient.parse_analytics(body)

    assert first.post_id == "6aaaf03e752fd8f8f102ef05", "hay que casar por el id de Zernio"
    assert first.external_post_id == "768621995457151312"
    assert (first.views, first.likes, first.comments, first.shares) == (1200, 90, 7, 4)
    assert first.profile_views == 15 and first.completion_rate == 0.42
    assert first.engagement_rate == 8.42
    assert first.updated_at == datetime(2026, 9, 17, 6, 0, tzinfo=timezone.utc)
    assert first.measured

    # Without a Zernio id it still resolves, by the platform's.
    assert second.post_id == "999" and second.external_post_id == "999"
    assert not second.measured, "pendiente de sincronizar no es «cero visitas»"


def test_analytics_parsing_survives_a_response_with_no_usable_ids():
    from app.services.zernio import ZernioClient

    assert ZernioClient.parse_analytics({"posts": [{"analytics": {"views": 5}}, "basura"]}) == []
    assert ZernioClient.parse_analytics({}) == []


# --------------------------------------------------------------------------- #
# A post that never finishes
# --------------------------------------------------------------------------- #

async def _stuck_processing(client, sample_video) -> tuple[int, dict]:
    """A video Zernio accepted and then lost: it stays «procesando» for ever."""
    await reset_mock()
    await configure_mock(url_polls=10_000)
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    video = await upload_sample(client, account_id, sample_video)
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await tick()
    item = await first_video(client)
    assert item["status"] == "PROCESSING"
    async with httpx.AsyncClient() as zernio:
        gone = await zernio.delete(f"{MOCK_BASE}/v1/posts/{item['zernio_post_id']}",
                                   headers={"Authorization": f"Bearer {ZERNIO_KEY}"})
        assert gone.status_code == 200
    return account_id, item


def _age(video_id: int, *, upload_minutes: int, status: str | None = None) -> None:
    from app.db import session_scope
    from app.models import VideoItem, VideoStatus

    with session_scope() as session:
        row = session.get(VideoItem, video_id)
        row.upload_started_at = datetime.now(timezone.utc) - timedelta(minutes=upload_minutes)
        row.next_status_check_at = None
        if status:
            row.status = VideoStatus(status)


async def test_a_processing_video_zernio_lost_fails_after_the_deadline(client, sample_video):
    """A 404 for ever used to keep the account «procesando» and block every upload."""
    _, item = await _stuck_processing(client, sample_video)
    _age(item["id"], upload_minutes=5)
    await tick()
    assert (await first_video(client))["status"] == "PROCESSING", "aún en plazo: se reintenta"

    _age(item["id"], upload_minutes=61)
    await tick()
    item = await first_video(client)
    assert item["status"] == "FAILED"
    assert "404" in item["last_error"]


async def test_a_stuck_processing_video_can_be_removed(client, sample_video):
    account_id, item = await _stuck_processing(client, sample_video)
    assert item["created_local"], "la cola muestra cuándo se añadió"
    resp = await client.delete(f"/api/videos/{item['id']}")
    assert resp.status_code == 200, resp.text
    assert (await account_entry(client, account_id))["videos"] == []


async def test_an_upload_in_progress_cannot_be_removed_but_a_stale_one_can(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    video = await upload_sample(client, account_id, sample_video)

    _age(video["id"], upload_minutes=1, status="UPLOADING")
    resp = await client.delete(f"/api/videos/{video['id']}")
    assert resp.status_code == 409, "puede estar subiéndose ahora mismo"

    _age(video["id"], upload_minutes=61, status="UPLOADING")
    resp = await client.delete(f"/api/videos/{video['id']}")
    assert resp.status_code == 200, resp.text
    assert (await account_entry(client, account_id))["videos"] == []
