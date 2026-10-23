"""The comment inbox: mirroring TikTok's comments and answering them.

Running on 127.0.0.1 there is no webhook, so everything here is polled. The
tests drive the polling explicitly through /api/comments/refresh rather than
waiting for the worker's own clock.
"""
from __future__ import annotations

from tests.helpers import (
    comment_payload,
    configure_mock,
    drain,
    first_video,
    make_account,
    mock_state,
    put_schedule,
    reset_mock,
    seed_comments,
    upload_sample,
    video_id_of,
)


async def _published_video(client, sample_video) -> tuple[int, dict, str]:
    """An account with one published video. Returns (account, video, tiktok id)."""
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    video = await upload_sample(client, account_id, sample_video)
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)
    item = await first_video(client)
    assert item["status"] == "PUBLISHED", item["last_error"]
    return account_id, item, await video_id_of(item["post_url"])


async def test_comments_are_mirrored_with_their_replies(client, sample_video):
    await reset_mock()
    _, item, video_id = await _published_video(client, sample_video)
    await seed_comments(video_id, [
        comment_payload("c1", "¡qué bueno!", username="ana", replies=[
            comment_payload("c1r1", "gracias", username="relay_user", is_owner=True),
        ]),
        comment_payload("c2", "¿dónde es esto?", username="luis"),
    ])

    resp = await client.post("/api/comments/refresh")
    assert resp.status_code == 200, resp.text
    assert resp.json()["new"] == 3  # two top-level plus the inline reply

    payload = (await client.get("/api/comments")).json()
    by_id = {c["external_id"]: c for c in payload["comments"]}
    assert set(by_id) == {"c1", "c1r1", "c2"}
    assert by_id["c1"]["author"] == "ana"
    assert by_id["c1"]["message"] == "¡qué bueno!"
    assert by_id["c1r1"]["is_owner"] is True
    assert by_id["c1r1"]["parent_external_id"] == "c1"
    assert by_id["c1"]["video_id"] == item["id"]

    # An already-answered comment is not pending; the unanswered one is.
    assert by_id["c1"]["answered"] is True
    assert by_id["c2"]["answered"] is False
    assert payload["pending"] == 1
    assert (await client.get("/api/state")).json()["comments_pending"] == 1
    assert (await first_video(client))["comment_count"] == 3

    # The account's own («Tú») can be left out of the list; they are still counted.
    others = (await client.get("/api/comments", params={"mine": "false"})).json()
    assert {c["external_id"] for c in others["comments"]} == {"c1", "c2"}
    assert others["own"] == payload["own"] == 1
    assert others["pending"] == 1


async def test_the_same_comment_is_not_stored_twice(client, sample_video):
    await reset_mock()
    _, _, video_id = await _published_video(client, sample_video)
    await seed_comments(video_id, [comment_payload("c1", "hola")])

    assert (await client.post("/api/comments/refresh")).json()["new"] == 1
    # A second pass inside the poll interval is skipped entirely.
    assert (await client.post("/api/comments/refresh")).json()["new"] == 0

    from app.db import session_scope
    from app.models import VideoItem

    with session_scope() as session:
        session.query(VideoItem).one().comments_checked_at = None
    assert (await client.post("/api/comments/refresh")).json()["new"] == 0
    assert len((await client.get("/api/comments")).json()["comments"]) == 1


async def test_replying_marks_the_comment_answered_and_reaches_tiktok(client, sample_video):
    await reset_mock()
    _, _, video_id = await _published_video(client, sample_video)
    await seed_comments(video_id, [comment_payload("c1", "¿qué cámara usas?")])
    await client.post("/api/comments/refresh")

    (comment,) = [c for c in (await client.get("/api/comments")).json()["comments"]
                  if c["external_id"] == "c1"]
    resp = await client.post(f"/api/comments/{comment['id']}/reply",
                             json={"message": "Una iPhone 17 :)"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["answered"] is True

    sent = (await mock_state())["comments"][video_id]
    mine = [c for c in sent if c["from"]["isOwner"]]
    assert [c["message"] for c in mine] == ["Una iPhone 17 :)"]
    assert mine[0]["parentId"] == "c1"

    # It is stored locally too, so the thread reads correctly offline.
    payload = (await client.get("/api/comments")).json()
    assert payload["pending"] == 0
    assert any(c["is_owner"] and c["message"] == "Una iPhone 17 :)"
               for c in payload["comments"])


async def test_an_empty_reply_is_refused(client, sample_video):
    await reset_mock()
    _, _, video_id = await _published_video(client, sample_video)
    await seed_comments(video_id, [comment_payload("c1", "hola")])
    await client.post("/api/comments/refresh")
    (comment,) = [c for c in (await client.get("/api/comments")).json()["comments"]
                  if c["external_id"] == "c1"]
    resp = await client.post(f"/api/comments/{comment['id']}/reply", json={"message": "   "})
    assert resp.status_code == 422


async def test_a_comment_can_be_hidden_and_shown_again(client, sample_video):
    await reset_mock()
    _, _, video_id = await _published_video(client, sample_video)
    await seed_comments(video_id, [comment_payload("c1", "spam spam spam")])
    await client.post("/api/comments/refresh")
    (comment,) = [c for c in (await client.get("/api/comments")).json()["comments"]
                  if c["external_id"] == "c1"]

    resp = await client.post(f"/api/comments/{comment['id']}/hide", json={"hidden": True})
    assert resp.status_code == 200 and resp.json()["hidden"] is True
    assert (await mock_state())["comments"][video_id][0]["hidden"] is True
    # Hidden comments drop out of the pending count: they need no answer.
    assert (await client.get("/api/comments")).json()["pending"] == 0

    resp = await client.post(f"/api/comments/{comment['id']}/hide", json={"hidden": False})
    assert resp.status_code == 200 and resp.json()["hidden"] is False
    assert (await mock_state())["comments"][video_id][0]["hidden"] is False


async def test_a_developer_app_account_is_told_to_reconnect(client, sample_video):
    """The inbox only answers on the TikTok for Business app lane."""
    await reset_mock()
    await _published_video(client, sample_video)
    await configure_mock(inbox_lane=False)

    resp = await client.post("/api/comments/refresh")
    assert resp.status_code == 502
    assert "app de desarrollador" in resp.json()["detail"]
    assert "Reconéctala" in resp.json()["detail"]


async def test_a_video_with_no_url_yet_is_not_polled(client, sample_video):
    """Without the TikTok video id there is nothing to ask the inbox about."""
    await reset_mock()
    await configure_mock(url_polls=10_000)
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    video = await upload_sample(client, account_id, sample_video)
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)

    resp = await client.post("/api/comments/refresh")
    assert resp.status_code == 200 and resp.json()["new"] == 0


async def test_a_comment_can_be_marked_answered_by_hand(client, sample_video):
    """Answered in the TikTok app, or nothing to say: no reply is sent."""
    await reset_mock()
    _, _, video_id = await _published_video(client, sample_video)
    await seed_comments(video_id, [comment_payload("c1", "🔥🔥🔥")])
    await client.post("/api/comments/refresh")
    (comment,) = [c for c in (await client.get("/api/comments")).json()["comments"]
                  if c["external_id"] == "c1"]

    resp = await client.post(f"/api/comments/{comment['id']}/answered", json={"answered": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["answered"] is True
    assert (await client.get("/api/comments")).json()["pending"] == 0
    assert not any(c["from"]["isOwner"]
                   for c in (await mock_state())["comments"].get(video_id, []))

    # The next sync does not undo it.
    from app.db import session_scope
    from app.models import VideoItem

    with session_scope() as session:
        session.query(VideoItem).one().comments_checked_at = None
    await client.post("/api/comments/refresh")
    assert (await client.get("/api/comments")).json()["pending"] == 0

    resp = await client.post(f"/api/comments/{comment['id']}/answered", json={"answered": False})
    assert resp.json()["answered"] is False
    assert (await client.get("/api/comments")).json()["pending"] == 1


async def test_every_comment_says_which_account_it_belongs_to(client, sample_video):
    await reset_mock()
    account_id, _, video_id = await _published_video(client, sample_video)
    await seed_comments(video_id, [comment_payload("c1", "hola")])
    await client.post("/api/comments/refresh")

    account = next(a["account"] for a in (await client.get("/api/state")).json()["accounts"]
                   if a["account"]["id"] == account_id)
    (comment,) = (await client.get("/api/comments")).json()["comments"]
    assert comment["account_id"] == account_id
    assert comment["account_label"] == account["label"]
    assert comment["account_platform"] == "tiktok"
    # And the list can be narrowed to one account.
    other = (await client.get("/api/comments", params={"account_id": account_id + 999})).json()
    assert other["comments"] == []
