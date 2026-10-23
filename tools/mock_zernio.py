"""A fake Zernio API, faithful enough to exercise the whole publish flow.

Point the app at it and every code path — key validation, presign, streamed
upload, post creation with the consent flags, status polling, analytics and the
comment inbox — runs for real against a local server:

    ZERNIO_API_BASE=http://127.0.0.1:8899/v1 ./.venv/bin/python -m app.main

It also hosts a notification receiver at /notify, to check outgoing webhooks,
and a fake Ollama (/api/tags, /api/chat) so the caption pipeline can be tested
without a language model.

Run this server with:  ./.venv/bin/python tools/mock_zernio.py
"""
from __future__ import annotations

import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI(title="Mock Zernio API")

MEDIA: dict[str, dict] = {}
POSTS: dict[str, dict] = {}
NOTIFY_CALLS: list[dict] = []
# Post ids cancelled through DELETE /v1/posts/{id}.
CANCELLED: list[str] = []
# TikTok video id -> comments, as the inbox would return them.
COMMENTS: dict[str, list[dict]] = {}

ACCOUNTS = [
    {"_id": "acc_tiktok_1", "platform": "tiktok", "username": "@relay_user",
     "displayName": "Relay User", "profileId": {"_id": "prof_1", "name": "Default"},
     "isActive": True, "profileUrl": "https://www.tiktok.com/@relay_user"},
    {"_id": "acc_tiktok_2", "platform": "tiktok", "username": "@segunda_cuenta",
     "displayName": "Segunda cuenta", "profileId": {"_id": "prof_1", "name": "Default"},
     "isActive": True, "profileUrl": "https://www.tiktok.com/@segunda_cuenta"},
    {"_id": "acc_youtube_1", "platform": "youtube", "username": "@relay_canal",
     "displayName": "Relay Canal", "profileId": {"_id": "prof_1", "name": "Default"},
     "isActive": True, "profileUrl": "https://www.youtube.com/@relay_canal"},
    {"_id": "acc_instagram_1", "platform": "instagram", "username": "@relay_user",
     "displayName": "Relay User", "profileId": {"_id": "prof_1", "name": "Default"},
     "isActive": True},
]

# Tunable behaviour, so tests can force failure paths.
CONFIG = {
    "key": "zernio-test-key",
    "fail": None,          # None | 400 | 401 | 402 | 429 | 500 | "failed"
    "url_polls": 1,        # status polls before platformPostUrl appears
    # Zernio documents ``publishedAt`` on its webhook payload but not on the
    # REST post, and does not send it there today: None reproduces that. Set
    # it to an ISO-8601 string to check that the app prefers Zernio's own
    # instant over anything it would infer.
    "published_at": None,
    "hide_accounts": [],   # account ids to omit from /accounts (simulates a disconnect)
    # Per-post metrics handed back by /v1/analytics, oldest post first. An empty
    # list means "no numbers yet", which is what a fresh post looks like.
    "analytics": [],
    # False reproduces an account still on TikTok's developer app: the whole
    # inbox answers 400 PLATFORM_LIMITATION.
    "inbox_lane": True,
    # The fake Ollama: what /api/chat answers (the three lines of a
    # description and its tags). "ollama_broken" makes it return something
    # that is not the JSON we asked for; "ollama_replies", when not empty, is
    # answered one per call, in order (a dict is sent as JSON, a str as is).
    "ollama_title": "Cinco hábitos para rendir más cada día",
    "ollama_caption": "Cinco hábitos que sí cambian el día 🔥",
    "ollama_fact": "Madrugar, la disciplina como rutina y tres cambios que se notan en una semana.",
    "ollama_question": "¿Cuál de los cinco te cuesta más?",
    "ollama_hashtags": ["habitos", "productividad", "rutina"],
    "ollama_changes": ["quitado el contador de la serie"],
    "ollama_broken": False,
    "ollama_replies": [],
    # True makes a scheduled post answer as already published, so a test does
    # not have to wait for its hour.
    "release_scheduled": False,
    # True makes DELETE /posts/{id} fail, as when Zernio is down.
    "fail_delete": False,
    # Post ids Zernio has already published: they read as published and
    # refuse DELETE with a 400, as the real API does.
    "published_ids": [],
}
DEFAULT_CONFIG = {k: (list(v) if isinstance(v, list) else v) for k, v in CONFIG.items()}


@app.get("/_mock/config")
def get_config() -> dict:
    return CONFIG


@app.post("/_mock/config")
async def set_config(request: Request) -> dict:
    CONFIG.update(await request.json())
    return CONFIG


@app.get("/_mock/state")
def mock_state() -> dict:
    return {
        "posts": POSTS,
        "media": {k: {kk: vv for kk, vv in v.items()} for k, v in MEDIA.items()},
        "notify_calls": NOTIFY_CALLS,
        "cancelled": CANCELLED,
        "comments": COMMENTS,
        "ollama_calls": OLLAMA_CALLS,
    }


@app.post("/_mock/reset")
def reset() -> dict:
    """Wipe state and restore every tunable, so no test leaks into the next."""
    MEDIA.clear()
    POSTS.clear()
    NOTIFY_CALLS.clear()
    CANCELLED.clear()
    COMMENTS.clear()
    OLLAMA_CALLS.clear()
    CONFIG.clear()
    CONFIG.update({k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_CONFIG.items()})
    return {"ok": True}


def _auth(request: Request) -> JSONResponse | None:
    header = request.headers.get("authorization", "")
    key = header[7:] if header.lower().startswith("bearer ") else ""
    if key != CONFIG["key"]:
        return JSONResponse({"error": "Unauthorized", "code": "unauthorized"}, 401)
    return None


def _forced_failure() -> JSONResponse | None:
    fail = CONFIG["fail"]
    if fail == 400:
        return JSONResponse({"error": "invalid_field_value",
                             "details": "tiktokSettings.privacy_level"}, 400)
    if fail == 401:
        return JSONResponse({"error": "Unauthorized"}, 401)
    if fail == 402:
        return JSONResponse({"error": "Payment Required", "reason": "account limit reached",
                             "dashboard_url": "https://zernio.com/dashboard"}, 402)
    if fail == 429:
        return JSONResponse({"error": "Rate limit exceeded. Please retry after 12 seconds.",
                             "details": {"retryAfterSeconds": 12}}, 429)
    if fail == 500:
        return JSONResponse({"error": "Internal error"}, 500)
    return None


@app.get("/v1/accounts")
def accounts(request: Request, platform: str = ""):
    if denied := _auth(request):
        return denied
    visible = [a for a in ACCOUNTS if a["_id"] not in CONFIG["hide_accounts"]]
    if platform:
        visible = [a for a in visible if a["platform"] == platform]
    return JSONResponse({"accounts": visible, "hasAnalyticsAccess": False})


@app.post("/v1/media/presign")
async def presign(request: Request):
    if denied := _auth(request):
        return denied
    if forced := _forced_failure():
        return forced
    body = await request.json()
    for key in ("filename", "contentType", "size"):
        if key not in body:
            return JSONResponse({"error": "invalid_request_body", "details": key}, 400)
    key = f"temp/{secrets.token_hex(8)}/{body['filename']}"
    MEDIA[key] = {"size": int(body["size"]), "contentType": body["contentType"],
                  "received": 0, "uploaded": False}
    # The host it was called with: in Docker the demo reaches it as mock-zernio.
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "uploadUrl": f"{base}/upload/{key}",
        "publicUrl": f"https://media.mock.invalid/{key}",
        "key": key,
        "expiresIn": 3600,
    })


@app.put("/upload/{key:path}")
async def upload(key: str, request: Request):
    entry = MEDIA.get(key)
    if entry is None:
        return JSONResponse({"error": "unknown key"}, 404)
    # R2/S3 presigned PUTs reject chunked transfer encoding.
    if "content-length" not in request.headers:
        return JSONResponse({"error": "Content-Length requerido"}, 411)
    if request.headers.get("content-type") != entry["contentType"]:
        return JSONResponse({"error": "Content-Type no coincide con el presign"}, 403)
    body = await request.body()
    if len(body) != entry["size"]:
        return JSONResponse({"error": f"tamaño {len(body)} != {entry['size']}"}, 400)
    entry["received"] = len(body)
    entry["uploaded"] = True
    return Response(status_code=200)


def _platform_settings(body: dict, platform: str) -> dict:
    platforms = body.get("platforms") or []
    if platforms:
        specific = (platforms[0].get("platformSpecificData") or {}).get(f"{platform}Settings")
        if specific:
            return specific
    return body.get(f"{platform}Settings") or {}


def _post_view(entry: dict) -> dict:
    url = entry["url"] if entry["polls"] >= CONFIG["url_polls"] else None
    platform = entry.get("platform", "tiktok")
    if CONFIG["fail"] == "failed":
        return {"post": {"_id": entry["id"], "status": "failed", "platforms": [{
            "platform": platform, "accountId": entry["account_id"], "status": "failed",
            "error": "The platform rejected the video (mock)"}]}}
    # A post with a future scheduledFor is held, exactly as Zernio holds it,
    # until CONFIG["release_scheduled"] says the hour has come.
    if (entry.get("scheduled_for") and not CONFIG["release_scheduled"]
            and entry["id"] not in CONFIG["published_ids"]):
        return {"post": {"_id": entry["id"], "status": "scheduled",
                         "scheduledFor": entry["scheduled_for"],
                         "timezone": entry.get("timezone"),
                         "platforms": [{"platform": platform,
                                        "accountId": entry["account_id"],
                                        "status": "scheduled"}]}}
    post = {"_id": entry["id"], "status": "published", "platforms": [{
        "platform": platform, "accountId": entry["account_id"], "status": "success",
        "platformPostUrl": url}], "platformPostUrl": url}
    if CONFIG["published_at"]:
        post["publishedAt"] = CONFIG["published_at"]
    return {"post": post}


@app.post("/v1/posts")
async def create_post(request: Request):
    if denied := _auth(request):
        return denied
    if forced := _forced_failure():
        return forced
    body = await request.json()
    media = body.get("mediaItems") or []
    if not media or not media[0].get("url"):
        return JSONResponse({"error": "invalid_request_body", "details": "mediaItems"}, 400)
    key = media[0]["url"].split("media.mock.invalid/", 1)[-1]
    if not MEDIA.get(key, {}).get("uploaded"):
        return JSONResponse({"error": "invalid_field_value",
                             "details": "mediaItems[0].url no está subido"}, 400)
    thumbnail = media[0].get("thumbnail")
    thumbnail_key = thumbnail.split("media.mock.invalid/", 1)[-1] if thumbnail else None
    if thumbnail_key and not MEDIA.get(thumbnail_key, {}).get("uploaded"):
        return JSONResponse({"error": "invalid_field_value",
                             "details": "mediaItems[0].thumbnail no está subido"}, 400)
    platforms = body.get("platforms") or []
    if not platforms or not platforms[0].get("platform"):
        return JSONResponse({"error": "invalid_request_body", "details": "platforms"}, 400)
    platform = platforms[0]["platform"]
    account_id = platforms[0].get("accountId")
    known = {a["_id"] for a in ACCOUNTS if a["platform"] == platform
             and a["_id"] not in CONFIG["hide_accounts"]}
    if account_id not in known:
        return JSONResponse({"error": "Resource not found", "details": "accountId"}, 404)
    settings = _platform_settings(body, platform)
    cover = settings.get("video_cover_image_url")
    if cover:
        thumbnail_key = cover.split("media.mock.invalid/", 1)[-1]
        if not MEDIA.get(thumbnail_key, {}).get("uploaded"):
            return JSONResponse({"error": "invalid_field_value",
                                 "details": f"{platform}Settings.video_cover_image_url no está subido"}, 400)
    for flag in ("content_preview_confirmed", "express_consent_given"):
        if settings.get(flag) is not True:
            return JSONResponse({"error": "invalid_field_value",
                                 "details": f"{platform}Settings.{flag} must be true"}, 400)
    # The TikTok for Business app lane publishes videos publicly and nothing else.
    if settings.get("privacy_level") != "PUBLIC_TO_EVERYONE":
        return JSONResponse({"error": "invalid_field_value",
                             "details": f"{platform}Settings.privacy_level"}, 400)
    if not body.get("publishNow") and not body.get("scheduledFor"):
        return JSONResponse({"error": "invalid_request_body",
                             "details": "publishNow or scheduledFor"}, 400)

    post_id = f"zp_{secrets.token_hex(8)}"
    username = next(a["username"] for a in ACCOUNTS if a["_id"] == account_id).lstrip("@")
    video_id = str(secrets.randbelow(10**18))
    url = f"https://www.tiktok.com/@{username}/video/{video_id}" if platform == "tiktok" else f"https://www.youtube.com/watch?v={video_id}"
    POSTS[post_id] = {
        "id": post_id, "account_id": account_id, "polls": 0, "video_id": video_id,
        "platform": platform,
        "url": url,
        "content": body.get("content", ""), "settings": settings,
        "title": body.get("title"), "tags": body.get("tags"),
        "first_comment": body.get("firstComment"),
        "media_key": key,
        "thumbnail_key": thumbnail_key,
        "scheduled_for": body.get("scheduledFor"),
        "timezone": body.get("timezone"),
        "publish_now": bool(body.get("publishNow")),
    }
    return JSONResponse(_post_view(POSTS[post_id]), 201)


@app.delete("/v1/posts/{post_id}")
def delete_post(post_id: str, request: Request):
    """Cancel a post Zernio has not published yet."""
    if denied := _auth(request):
        return denied
    if CONFIG["fail_delete"]:
        return JSONResponse({"error": "Service unavailable"}, 503)
    if post_id in CONFIG["published_ids"]:
        return JSONResponse({"error": "Published posts cannot be deleted"}, 400)
    entry = POSTS.pop(post_id, None)
    if entry is None:
        return JSONResponse({"error": "Resource not found"}, 404)
    CANCELLED.append(post_id)
    return JSONResponse({"ok": True, "id": post_id, "status": "cancelled"})


@app.get("/v1/posts/{post_id}")
def get_post(post_id: str, request: Request):
    if denied := _auth(request):
        return denied
    entry = POSTS.get(post_id)
    if entry is None:
        return JSONResponse({"error": "Resource not found"}, 404)
    entry["polls"] += 1
    return JSONResponse(_post_view(entry))


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #

@app.get("/v1/analytics")
def analytics(request: Request, platform: str = "", accountId: str = "",  # noqa: N803
              page: int = 1, limit: int = 50, fromDate: str = "", toDate: str = ""):  # noqa: N803
    """Per-post numbers, taken from CONFIG["analytics"] in post creation order."""
    if denied := _auth(request):
        return denied
    if forced := _forced_failure():
        return forced
    entries = [p for p in POSTS.values()
               if not accountId or p["account_id"] == accountId]
    numbers = CONFIG["analytics"] or []
    posts = []
    for i, entry in enumerate(entries):
        data = numbers[i] if i < len(numbers) else {}
        posts.append({
            # The two ids Zernio really answers with: its own post id is
            # ``latePostId``, and ``postId`` is the platform's. It does not
            # send ``_id`` here, whatever it does on /posts.
            "postId": entry["video_id"],
            "latePostId": entry["id"],
            "platform": "tiktok",
            "status": "published",
            "syncStatus": "synced",
            "platformPostUrl": entry["url"],
            "analytics": {
                "views": data.get("views", 0),
                "likes": data.get("likes", 0),
                "comments": data.get("comments", 0),
                "shares": data.get("shares", 0),
                "profileViews": data.get("profileViews", 0),
                "completionRate": data.get("completionRate", 0),
                "engagementRate": data.get("engagementRate", 0),
                "lastUpdated": data.get("lastUpdated", "2026-09-16T08:00:00Z"),
            },
        })
    start = (max(page, 1) - 1) * limit
    window = posts[start:start + limit]
    return JSONResponse({
        "overview": {"totalPosts": len(posts)},
        "posts": window,
        "pagination": {"page": page, "limit": limit, "total": len(posts)},
        "hasAnalyticsAccess": True,
    })


# --------------------------------------------------------------------------- #
# Comment inbox
# --------------------------------------------------------------------------- #

def _lane_check() -> JSONResponse | None:
    if not CONFIG["inbox_lane"]:
        return JSONResponse(
            {"error": "This account is connected through TikTok's developer app",
             "code": "PLATFORM_LIMITATION"}, 400)
    return None


def _post_by_ref(ref: str) -> dict | None:
    """The inbox is addressed by TikTok video id; Zernio also resolves its own."""
    for entry in POSTS.values():
        if ref in (entry["video_id"], entry["id"]):
            return entry
    return None


@app.post("/_mock/comments/{video_id}")
async def seed_comments(video_id: str, request: Request) -> dict:
    """Test hook: put comments on a video as if TikTok users had written them."""
    body = await request.json()
    COMMENTS.setdefault(video_id, []).extend(body.get("comments") or [])
    return {"ok": True, "count": len(COMMENTS[video_id])}


@app.get("/v1/inbox/comments/{ref}")
def list_comments(ref: str, request: Request, accountId: str = "", limit: int = 50):  # noqa: N803
    if denied := _auth(request):
        return denied
    if lane := _lane_check():
        return lane
    if not accountId:
        return JSONResponse({"error": "invalid_request_body", "details": "accountId"}, 400)
    entry = _post_by_ref(ref)
    if entry is None:
        return JSONResponse({"error": "Resource not found", "details": "postId"}, 404)
    return JSONResponse({
        "status": "ok",
        "comments": COMMENTS.get(entry["video_id"], [])[:limit],
        "pagination": {"cursor": None},
    })


@app.post("/v1/inbox/comments/{ref}")
async def write_comment(ref: str, request: Request):
    if denied := _auth(request):
        return denied
    if lane := _lane_check():
        return lane
    entry = _post_by_ref(ref)
    if entry is None:
        return JSONResponse({"error": "Resource not found", "details": "postId"}, 404)
    body = await request.json()
    if not body.get("accountId") or not body.get("message"):
        return JSONResponse({"error": "invalid_request_body",
                             "details": "accountId, message"}, 400)
    comment = {
        "id": f"c_{secrets.token_hex(6)}",
        "message": body["message"],
        "parentId": body.get("commentId"),
        "from": {"id": body["accountId"], "name": "Relay User",
                 "username": "relay_user", "isOwner": True},
        "likeCount": 0,
        "replyCount": 0,
        "createdTime": "2026-09-16T09:00:00Z",
        "platform": "tiktok",
    }
    COMMENTS.setdefault(entry["video_id"], []).append(comment)
    return JSONResponse({"status": "ok", "comment": comment}, 201)


@app.post("/v1/inbox/comments/{ref}/{comment_id}/hide")
async def hide_comment(ref: str, comment_id: str, request: Request):
    if denied := _auth(request):
        return denied
    if lane := _lane_check():
        return lane
    entry = _post_by_ref(ref)
    if entry is None:
        return JSONResponse({"error": "Resource not found"}, 404)
    for comment in COMMENTS.get(entry["video_id"], []):
        if comment["id"] == comment_id:
            comment["hidden"] = True
            return JSONResponse({"status": "ok", "commentId": comment_id, "hidden": True,
                                 "platform": "tiktok"})
    return JSONResponse({"error": "Resource not found", "details": "commentId"}, 404)


@app.delete("/v1/inbox/comments/{ref}/{comment_id}/hide")
def unhide_comment(ref: str, comment_id: str, request: Request, accountId: str = ""):  # noqa: N803
    if denied := _auth(request):
        return denied
    if lane := _lane_check():
        return lane
    entry = _post_by_ref(ref)
    if entry is None:
        return JSONResponse({"error": "Resource not found"}, 404)
    for comment in COMMENTS.get(entry["video_id"], []):
        if comment["id"] == comment_id:
            comment["hidden"] = False
            return JSONResponse({"status": "ok", "commentId": comment_id, "hidden": False,
                                 "platform": "tiktok"})
    return JSONResponse({"error": "Resource not found", "details": "commentId"}, 404)


# --------------------------------------------------------------------------- #
# Fake Ollama
# --------------------------------------------------------------------------- #

OLLAMA_CALLS: list[dict] = []


@app.get("/api/tags")
def ollama_tags():
    return JSONResponse({"models": [
        {"name": "mock-llm:latest", "model": "mock-llm:latest", "size": 1_000_000},
        {"name": "mock-big:latest", "model": "mock-big:latest", "size": 9_000_000_000},
    ]})


@app.post("/api/chat")
async def ollama_chat(request: Request):
    import json as _json
    body = await request.json()
    OLLAMA_CALLS.append(body)
    if CONFIG["ollama_broken"]:
        content = "Claro, aquí tienes un texto genial para tu vídeo."
    elif CONFIG["ollama_replies"]:
        reply = CONFIG["ollama_replies"].pop(0)
        content = reply if isinstance(reply, str) else _json.dumps(reply, ensure_ascii=False)
    else:
        reply = {"titulo": CONFIG["ollama_title"],
                 "gancho": CONFIG["ollama_caption"], "dato": CONFIG["ollama_fact"],
                 "pregunta": CONFIG["ollama_question"], "hashtags": CONFIG["ollama_hashtags"]}
        system = (body.get("messages") or [{}])[0].get("content", "")
        if "ficha" in system:
            reply["cambios"] = CONFIG["ollama_changes"]
        content = _json.dumps(reply, ensure_ascii=False)
    return JSONResponse({"model": body.get("model"), "done": True,
                         "message": {"role": "assistant", "content": content}})


@app.post("/notify")
@app.post("/notify/{rest:path}")
async def notify(request: Request, rest: str = ""):
    raw = await request.body()
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - plain-text bodies (ntfy)
        payload = raw.decode("utf-8", "replace")
    NOTIFY_CALLS.append({
        "path": request.url.path, "query": request.url.query,
        "headers": {k: v for k, v in request.headers.items()
                    if k in ("title", "priority", "tags", "content-type")},
        "payload": payload,
    })
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import os

    import uvicorn

    # 0.0.0.0 inside Docker (docker-compose.yml, profile demo); loopback otherwise.
    uvicorn.run(app, host=os.environ.get("MOCK_HOST", "127.0.0.1"), port=8899, log_level="warning")
