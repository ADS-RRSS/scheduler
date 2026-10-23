"""Shared helpers for the API-level tests."""
from __future__ import annotations

from pathlib import Path

import httpx

MOCK_BASE = "http://127.0.0.1:8899"
ZERNIO_KEY = "zernio-test-key"
ACCOUNT_1 = "acc_tiktok_1"
ACCOUNT_2 = "acc_tiktok_2"
NOTIFY_URL = f"{MOCK_BASE}/notify"


async def reset_mock() -> None:
    async with httpx.AsyncClient() as c:
        await c.post(f"{MOCK_BASE}/_mock/reset")


async def configure_mock(**options) -> None:
    async with httpx.AsyncClient() as c:
        await c.post(f"{MOCK_BASE}/_mock/config", json=options)


async def mock_state() -> dict:
    async with httpx.AsyncClient() as c:
        return (await c.get(f"{MOCK_BASE}/_mock/state")).json()


async def make_account(client: httpx.AsyncClient, zernio_id: str = ACCOUNT_1,
                       label: str = "") -> int:
    """Connect one TikTok account through the fake Zernio and return its id."""
    body = {"api_key": ZERNIO_KEY, "account_ids": [zernio_id]}
    if label:
        body["labels"] = {zernio_id: label}
    resp = await client.post("/api/accounts", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["added"][0]["id"]


async def account_entry(client: httpx.AsyncClient, account_id: int) -> dict:
    """The /api/state block of one account."""
    state = (await client.get("/api/state")).json()
    for entry in state["accounts"]:
        if entry["account"]["id"] == account_id:
            return entry
    raise AssertionError(f"la cuenta {account_id} no está en /api/state")


async def default_queue_id(client: httpx.AsyncClient, account_id: int) -> int:
    """The id of the account's first queue, the one it is created with."""
    entry = await account_entry(client, account_id)
    return entry["queues"][0]["schedule"]["id"]


async def add_queue(
    client: httpx.AsyncClient, account_id: int, name: str = "", copy_from: int | None = None
) -> int:
    """Add a second (third, ...) queue to an account and return its id."""
    body: dict = {"name": name}
    if copy_from is not None:
        body["copy_from"] = copy_from
    resp = await client.post(f"/api/accounts/{account_id}/schedules", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["schedule"]["id"]


async def upload_to_library(
    client: httpx.AsyncClient, video: Path, caption: str | None = None, name: str | None = None,
) -> dict:
    """Upload a video (with a hand-written .txt when ``caption``) and return its library row.

    The same bytes uploaded again come back as the existing row: the library
    keeps one copy per content.
    """
    name = name or video.name
    files = [("files", (name, video.read_bytes(), "video/mp4"))]
    if caption:
        files.append(("files", (Path(name).with_suffix(".txt").name, caption.encode(),
                                "text/plain")))
    resp = await client.post("/api/library/upload", files=files)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    if payload["added"]:
        return payload["added"][0]
    assert payload["duplicates"], payload["problems"]
    return (await client.get(f"/api/library/{payload['duplicates'][0]['library_id']}")).json()


async def enqueue(client: httpx.AsyncClient, queue_id: int, library_id: int,
                  rewrite: bool = False) -> httpx.Response:
    return await client.post(f"/api/schedules/{queue_id}/videos",
                             json={"library_ids": [library_id], "rewrite": rewrite})


async def upload_to_queue(
    client: httpx.AsyncClient, queue_id: int, video: Path, caption: str = "Prueba [hashtags]"
) -> dict:
    """Upload to the library, then add it to the queue: what the UI does in two steps."""
    item = await upload_to_library(client, video, caption)
    resp = await enqueue(client, queue_id, item["id"])
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["added"], payload["problems"]
    return payload["added"][0]


async def upload_sample(
    client: httpx.AsyncClient, account_id: int, video: Path, caption: str = "Prueba [hashtags]"
) -> dict:
    """Upload into the account's first queue."""
    return await upload_to_queue(
        client, await default_queue_id(client, account_id), video, caption
    )


async def put_schedule(client: httpx.AsyncClient, account_id: int, **overrides) -> dict:
    """Configure the account's first queue."""
    return await put_queue_schedule(
        client, await default_queue_id(client, account_id), **overrides
    )


async def put_queue_schedule(client: httpx.AsyncClient, queue_id: int, **overrides) -> dict:
    body = {
        "enabled": True,
        "interval_min_minutes": 25,
        "interval_max_minutes": 35,
        "jitter_seconds": 0,
        "max_per_day": None,
        "windows": [],
        "days_of_week": [0, 1, 2, 3, 4, 5, 6],
        "timezone": "Europe/Madrid",
        "disable_comment": False,
        "disable_duet": False,
        "disable_stitch": False,
        "is_aigc": False,
        "brand_content_toggle": False,
        "brand_organic_toggle": False,
    }
    body.update(overrides)
    resp = await client.put(f"/api/schedules/{queue_id}", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def tick() -> None:
    from app.scheduler.worker import worker
    await worker.tick()


async def drain(account_id: int, max_ticks: int = 12) -> None:
    """Run worker ticks until nothing is in flight in any queue of the account."""
    from app.db import session_scope
    from app.models import Account, VideoStatus
    from app.scheduler.worker import worker
    from app.services import queue

    for _ in range(max_ticks):
        await worker.tick()
        with session_scope() as session:
            account = session.get(Account, account_id)
            busy = queue.in_flight(session, account_id)
            pending = [
                v
                for schedule in (account.schedules if account else [])
                for v in queue.pending(session, schedule.id)
                if v.status == VideoStatus.SCHEDULED
            ]
        if not busy and not pending:
            return


async def first_video(client: httpx.AsyncClient, account_index: int = 0) -> dict:
    return (await client.get("/api/state")).json()["accounts"][account_index]["videos"][0]


async def seed_comments(video_id: str, comments: list[dict]) -> None:
    """Put comments on a video in the fake Zernio, as TikTok users would."""
    async with httpx.AsyncClient() as c:
        resp = await c.post(f"{MOCK_BASE}/_mock/comments/{video_id}",
                            json={"comments": comments})
        assert resp.status_code == 200, resp.text


def comment_payload(external_id: str, message: str, username: str = "fan",
                    is_owner: bool = False, parent: str | None = None,
                    replies: list[dict] | None = None) -> dict:
    """One comment shaped the way Zernio's inbox returns them."""
    payload = {
        "id": external_id,
        "message": message,
        "createdTime": "2026-09-16T08:30:00Z",
        "from": {"id": f"u_{username}", "name": username.title(),
                 "username": username, "isOwner": is_owner},
        "likeCount": 0,
        "replyCount": len(replies or []),
        "platform": "tiktok",
    }
    if parent:
        payload["parentId"] = parent
    if replies:
        payload["replies"] = replies
    return payload


async def video_id_of(post_url: str) -> str:
    """The TikTok video id the mock put in a post URL."""
    return post_url.rsplit("/", 1)[-1]
