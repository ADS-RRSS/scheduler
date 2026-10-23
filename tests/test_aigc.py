"""The AI-generated content label, which is now decided per video.

TikTok wants the disclosure on the video, so the decision travels with the
video: a queue hands its default to everything that enters it, and from then on
that video keeps what it was given even if the queue changes its mind.
"""
from __future__ import annotations

from tests.helpers import (
    account_entry,
    default_queue_id,
    drain,
    first_video,
    make_account,
    mock_state,
    put_queue_schedule,
    put_schedule,
    reset_mock,
    upload_sample,
    upload_to_queue,
)


async def test_a_new_queue_labels_its_videos_by_default(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue = (await account_entry(client, account_id))["queues"][0]["schedule"]
    assert queue["is_aigc"] is True, "una cola nueva marca el contenido como IA"

    video = await upload_sample(client, account_id, sample_video)
    assert video["is_aigc"] is True

    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)
    (post,) = (await mock_state())["posts"].values()
    assert post["settings"]["video_made_with_ai"] is True


async def test_one_video_can_be_taken_out_of_the_label(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    await put_schedule(client, account_id, is_aigc=True)
    video = await upload_sample(client, account_id, sample_video)

    resp = await client.patch(f"/api/videos/{video['id']}", json={"is_aigc": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_aigc"] is False

    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)
    (post,) = (await mock_state())["posts"].values()
    assert "video_made_with_ai" not in post["settings"]


async def test_a_video_keeps_the_label_its_queue_gave_it(client, sample_video):
    """Changing the queue must not relabel what is already waiting in it."""
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, is_aigc=True)
    video = await upload_sample(client, account_id, sample_video)
    assert video["is_aigc"] is True

    await put_queue_schedule(client, queue_id, is_aigc=False)
    entry = await account_entry(client, account_id)
    assert entry["queues"][0]["schedule"]["is_aigc"] is False
    assert (await first_video(client))["is_aigc"] is True

    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)
    (post,) = (await mock_state())["posts"].values()
    assert post["settings"]["video_made_with_ai"] is True


async def test_a_queue_without_the_label_leaves_its_videos_alone(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, is_aigc=False)

    video = await upload_to_queue(client, queue_id, sample_video)
    assert video["is_aigc"] is False

    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)
    (post,) = (await mock_state())["posts"].values()
    assert "video_made_with_ai" not in post["settings"]


async def test_clearing_the_override_follows_the_queue_again(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, is_aigc=True)
    video = await upload_sample(client, account_id, sample_video)

    await client.patch(f"/api/videos/{video['id']}", json={"is_aigc": False})
    resp = await client.patch(f"/api/videos/{video['id']}", json={"is_aigc": None})
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_aigc"] is None, "sin opinión propia: manda la cola"

    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)
    (post,) = (await mock_state())["posts"].values()
    assert post["settings"]["video_made_with_ai"] is True


async def test_the_label_travels_with_a_delegated_video(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, is_aigc=True, max_per_day=None)
    await upload_to_queue(client, queue_id, sample_video)

    await client.post(f"/api/schedules/{queue_id}/delegate", json={})
    from tests.helpers import tick

    await tick()
    (post,) = (await mock_state())["posts"].values()
    assert post["settings"]["video_made_with_ai"] is True
    assert post["scheduled_for"], "entregado con hora, no publicado"
