"""Client for the parts of Zernio's API this tool needs.

Zernio (formerly Late / getlate.dev) owns an audited TikTok app: you connect
your TikTok account to it once and it publishes on your behalf, so no TikTok
developer app, review or credentials are needed on your side. Free for the
first two connected accounts.

Endpoints used (all under https://zernio.com/api/v1, ``Authorization: Bearer``):

    GET  /accounts?platform=tiktok    connected TikTok accounts
    POST /media/presign               {filename, contentType, size}
    PUT  <uploadUrl>                  the bytes; Content-Type must match
    POST /posts                       the post, publishNow: true — or
                                      scheduledFor + timezone to hand the whole
                                      queue over and let Zernio release it
    GET  /posts/{id}                  status; platformPostUrl arrives later
    DELETE /posts/{id}                cancel a post Zernio has not published yet
    GET  /analytics?platform=tiktok   views/likes/comments/shares per post
    GET  /inbox/comments/{postId}     comments on one published video
    POST /inbox/comments/{postId}     write a comment or reply to one

The inbox endpoints only answer for accounts Zernio connected through TikTok's
Business app; an older developer-app connection returns 400 with code
``PLATFORM_LIMITATION``, which :class:`ZernioError` surfaces as ``zernio_inbox_
unavailable`` so the UI can tell the user to reconnect instead of retrying.

Nothing here touches the database: the publisher decides what a result means
for the queue. Every failure is a ``ZernioError`` that says whether retrying
is worth it.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import socket
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

log = logging.getLogger("scheduler")

READ_CHUNK = 8 * 1024 * 1024
DEFAULT_API_BASE = "https://zernio.com/api/v1"
DASHBOARD_URL = "https://zernio.com/dashboard"
API_KEYS_URL = "https://zernio.com/dashboard/api-keys"


class ZernioError(Exception):
    """A failure talking to Zernio. ``retryable`` drives the queue's retries."""

    def __init__(self, code: str, message: str = "", *, retryable: bool = False,
                 http_status: int | None = None, retry_after: int | None = None) -> None:
        self.code = code or "zernio_error"
        self.message = message or ""
        self.retryable = retryable
        self.http_status = http_status
        self.retry_after = retry_after
        super().__init__(f"[{self.code}] {self.message}".strip())


@dataclass
class PostResult:
    """Outcome of one publish or status call."""

    post_id: str | None = None
    post_url: str | None = None
    # "published" | "scheduled" | "processing" | "failed"
    status: str = "processing"
    message: str = ""
    raw: dict = field(default_factory=dict)
    # When Zernio says the post actually went out. Its webhook payload carries
    # ``publishedAt``; the documented REST shape of GET /posts/{id} does not,
    # so this is None more often than not and the caller has to fall back.
    published_at: datetime | None = None

    @property
    def published(self) -> bool:
        return self.status == "published"

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    @property
    def scheduled(self) -> bool:
        """Accepted and parked: Zernio will publish it at its ``scheduledFor``."""
        return self.status == "scheduled"


@dataclass(frozen=True)
class PostMetrics:
    """One post's numbers, as ``GET /analytics`` reports them.

    The endpoint names a post twice and the two ids are not interchangeable:
    ``latePostId`` is the post Zernio created for us (what ``POST /posts``
    returned and this tool stores), and ``postId`` is the platform's own —
    TikTok's numeric video id. Both are kept so the caller can match on
    whichever it holds.
    """

    post_id: str
    external_post_id: str = ""
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    # Business-app connections only; TikTok reports both 24-48 h late.
    profile_views: int = 0
    completion_rate: float = 0.0
    engagement_rate: float = 0.0
    updated_at: datetime | None = None
    # "synced" | "pending" | "partial" | "unavailable". Anything but the first
    # means the zeros below are "not measured yet", not "nobody watched".
    sync_status: str = ""

    @property
    def measured(self) -> bool:
        """Has the platform actually reported on this post yet?"""
        return self.sync_status in ("", "synced", "partial")

    @property
    def interactions(self) -> int:
        return self.likes + self.comments + self.shares


@dataclass(frozen=True)
class CommentItem:
    """One comment on a published video."""

    external_id: str
    message: str = ""
    author_name: str = ""
    author_username: str = ""
    like_count: int = 0
    reply_count: int = 0
    is_owner: bool = False
    parent_external_id: str | None = None
    created_at: datetime | None = None


def parse_dt(value) -> datetime | None:
    """ISO-8601 from Zernio (``...Z`` included) as an aware UTC datetime."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_VIDEO_ID_RE = re.compile(r"/video/(\d+)")


def tiktok_video_id(post_url: str | None) -> str | None:
    """The numeric id in https://www.tiktok.com/@user/video/<id>.

    The inbox endpoints want TikTok's own video id. Zernio also resolves its
    own post ids, so this is an optimisation, not a requirement.
    """
    if not post_url:
        return None
    match = _VIDEO_ID_RE.search(post_url)
    return match.group(1) if match else None


# TikTok's Business app, which is the lane Zernio connects through, only
# publishes videos publicly. Since v3 this tool does not pretend otherwise: it
# is a constant, not a setting.
PRIVACY_LEVEL = "PUBLIC_TO_EVERYONE"


@dataclass(frozen=True)
class PostOptions:
    """What the publisher wants; mapped onto Zernio's ``tiktokSettings``."""

    caption: str
    title: str = ""
    first_comment: str = ""
    tags: tuple[str, ...] = ()
    disable_comment: bool = False
    disable_duet: bool = False
    disable_stitch: bool = False
    brand_content_toggle: bool = False
    brand_organic_toggle: bool = False
    is_aigc: bool = False


def mime_for(path: Path) -> str:
    guess = mimetypes.guess_type(path.name)[0]
    return guess or {"mov": "video/quicktime", "webm": "video/webm"}.get(
        path.suffix.lower().lstrip("."), "video/mp4"
    )


async def _iter_file(path: Path) -> AsyncIterator[bytes]:
    """Stream a file in chunks without blocking the event loop."""
    def read(fh):
        return fh.read(READ_CHUNK)

    with path.open("rb") as fh:
        while True:
            chunk = await asyncio.to_thread(read, fh)
            if not chunk:
                break
            yield chunk


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


# --- Reaching the upload host --------------------------------------------- #
# The bytes do not go to zernio.com: presign hands out a URL on Cloudflare R2,
# which lives on a different IP prefix. Some ISPs blackhole whole Cloudflare
# prefixes - in Spain, court-ordered football blocks are the usual reason - and
# then this breaks in a way that looks like the app's fault: every API call
# answers and only the upload dies, on SYNs nobody ever replies to.
#
# Cloudflare's edge is anycast and picks the site by SNI, so any reachable edge
# address serves that same host. The presign response points at another
# Cloudflare host of the same service (``publicUrl``), so its addresses are the
# way back in, and nothing has to be hardcoded. The Host header and the SNI
# stay on the original name: the S3 signature covers Host and the certificate
# is checked against the name, so this changes the route and nothing else.
_EDGE_MEMORY_SECONDS = 900.0
_edge_routes: dict[str, tuple[str, float]] = {}


def _learnt_edge(host: str) -> str:
    """The address that got through to ``host`` recently, if any.

    Worth remembering: while a block lasts, every video in the queue would
    otherwise pay the connect timeout again before falling back.
    """
    address, until = _edge_routes.get(host, ("", 0.0))
    return address if until > time.monotonic() else ""


def _learn_edge(host: str, address: str) -> None:
    if address:
        _edge_routes[host] = (address, time.monotonic() + _EDGE_MEMORY_SECONDS)
    else:  # the name resolves to something reachable again
        _edge_routes.pop(host, None)


async def _addresses(host: str) -> list[str]:
    if not host:
        return []
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, 443, 0, socket.SOCK_STREAM)
    except OSError:
        return []
    return list(dict.fromkeys(info[4][0] for info in infos))


def _json(resp: httpx.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {"data": body}


class ZernioClient:
    def __init__(self, api_key: str, api_base: str = DEFAULT_API_BASE,
                 timeout: float = 600.0) -> None:
        self._key = api_key.strip()
        self._base = api_base.rstrip("/")
        self._timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", "Accept": "application/json"}

    def _raise(self, resp: httpx.Response, what: str) -> None:
        body = _json(resp)
        detail = body.get("error") or body.get("message") or resp.text[:200]
        if isinstance(body.get("details"), str):
            detail = f"{detail}: {body['details']}"
        status = resp.status_code
        code = body.get("code") or (body.get("error") if isinstance(body.get("error"), str)
                                    else "")
        if status == 400 and "PLATFORM_LIMITATION" in str(code).upper() + str(detail).upper():
            raise ZernioError(
                "zernio_inbox_unavailable",
                "Esta cuenta sigue conectada a Zernio por la app de desarrollador de TikTok, "
                "que no da acceso a los comentarios. Reconéctala en el panel de Zernio "
                f"({DASHBOARD_URL}) y vuelve a probar.",
                http_status=status)
        if status == 403:
            raise ZernioError(
                "zernio_inbox_forbidden",
                f"Zernio no da acceso al buzón con este plan: {detail}. Revisa {DASHBOARD_URL}",
                http_status=status)
        if status == 401:
            raise ZernioError("zernio_auth", "Zernio rechazó la API key (401). Genera otra en "
                              f"{API_KEYS_URL} y sustitúyela en Cuentas.", http_status=status)
        if status == 402:
            url = body.get("dashboard_url") or DASHBOARD_URL
            raise ZernioError("zernio_plan_limit",
                              f"Zernio pide una acción en tu plan: {detail}. Revisa {url}",
                              http_status=status)
        if status == 429:
            retry_after = None
            details = body.get("details")
            if isinstance(details, dict):
                retry_after = details.get("retryAfterSeconds")
            raise ZernioError("zernio_rate_limit", f"Límite de peticiones de Zernio: {detail}",
                              retryable=True, http_status=status,
                              retry_after=int(retry_after) if retry_after else None)
        if status == 409:
            raise ZernioError("zernio_duplicate",
                              f"Zernio detectó un post duplicado: {detail}", http_status=status)
        if 500 <= status < 600:
            raise ZernioError("zernio_server_error",
                              f"Zernio devolvió {status} en {what}: {detail}",
                              retryable=True, http_status=status)
        raise ZernioError("zernio_rejected", f"Zernio rechazó {what} ({status}): {detail}",
                          http_status=status)

    async def list_accounts(self, platform: str = "tiktok") -> list[dict]:
        """Connected accounts, normalised to what the UI needs."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(f"{self._base}/accounts", headers=self._headers,
                                        params={"platform": platform} if platform else None)
        except httpx.HTTPError as exc:
            raise ZernioError("zernio_network", f"no se pudo contactar con Zernio: {exc}",
                              retryable=True) from exc
        if resp.status_code >= 400:
            self._raise(resp, "la lista de cuentas")
        body = _json(resp)
        accounts = body.get("accounts")
        if accounts is None and isinstance(body.get("data"), list):
            accounts = body["data"]
        out: list[dict] = []
        for acc in accounts or []:
            if platform and acc.get("platform") and acc["platform"] != platform:
                continue
            profile = acc.get("profileId")
            out.append({
                "id": str(acc.get("_id") or acc.get("id") or ""),
                "platform": acc.get("platform") or platform,
                "username": (acc.get("username") or "").lstrip("@"),
                "display_name": acc.get("displayName") or acc.get("username") or "",
                "profile": (profile or {}).get("name") if isinstance(profile, dict) else profile,
                "is_active": acc.get("isActive", True),
                "profile_url": acc.get("profileUrl"),
            })
        return [a for a in out if a["id"]]

    async def presign(self, path: Path) -> dict:
        size = path.stat().st_size
        payload = {"filename": path.name, "contentType": mime_for(path), "size": size}
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(f"{self._base}/media/presign", headers=self._headers,
                                         json=payload)
        except httpx.HTTPError as exc:
            raise ZernioError("zernio_network", f"no se pudo contactar con Zernio: {exc}",
                              retryable=True) from exc
        if resp.status_code >= 400:
            self._raise(resp, "la reserva de subida")
        body = _json(resp)
        if not body.get("uploadUrl") or not body.get("publicUrl"):
            raise ZernioError("zernio_invalid_response",
                              f"presign sin uploadUrl/publicUrl: {str(body)[:200]}")
        return body

    async def _put_file(self, client: httpx.AsyncClient, upload_url: str, path: Path,
                        headers: dict[str, str], address: str = "") -> httpx.Response:
        """One PUT, optionally forced through ``address`` instead of DNS's answer."""
        if not address:
            return await client.put(upload_url, content=_iter_file(path), headers=headers)
        parts = urlsplit(upload_url)
        netloc = f"[{address}]" if ":" in address else address
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        return await client.put(
            urlunsplit((parts.scheme, netloc, parts.path, parts.query, "")),
            content=_iter_file(path),
            headers={**headers, "Host": parts.netloc},
            extensions={"sni_hostname": parts.hostname},
        )

    async def upload(self, upload_url: str, path: Path, *, edge_host: str = "") -> None:
        """Send the bytes to the presigned slot.

        ``edge_host`` is another host of the same service - ``publicUrl`` from
        the same presign response. When the upload host's own addresses are
        unreachable, its addresses are tried as a way in; see the note above
        :func:`_learnt_edge`.
        """
        size = path.stat().st_size
        # S3-style presigned PUTs refuse chunked encoding: an explicit
        # Content-Length makes httpx stream the body with a fixed length.
        headers = {"Content-Type": mime_for(path), "Content-Length": str(size)}
        host = urlsplit(upload_url).hostname or ""
        # A connection that has not landed in 15 s is not slow, it is blocked:
        # left alone, macOS spends 75 s on a blackholed route, for every video.
        timeout = httpx.Timeout(self._timeout, connect=15.0)
        failure: Exception | None = None
        resp: httpx.Response | None = None

        async with httpx.AsyncClient(timeout=timeout) as client:
            async def attempt(address: str) -> httpx.Response | None:
                nonlocal failure
                try:
                    return await self._put_file(client, upload_url, path, headers, address)
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    failure = exc
                    log.warning("Sin conexión con %s%s: %s", host,
                                f" por {address}" if address else "", exc)
                    return None

            # What worked last time, then whatever DNS answers now ("").
            for address in dict.fromkeys([_learnt_edge(host), ""]):
                resp = await attempt(address)
                if resp is not None:
                    _learn_edge(host, address)
                    break
            else:
                for address in await _addresses(edge_host):
                    resp = await attempt(address)
                    if resp is not None:
                        _learn_edge(host, address)
                        log.warning(
                            "Las direcciones de %s no son alcanzables desde esta conexión; "
                            "la subida ha entrado por %s (%s).", host, address, edge_host)
                        break

        if resp is None:
            raise ZernioError(
                "zernio_upload_unreachable",
                f"no hay ruta hasta el servidor de subida de Zernio ({host}). La API de Zernio "
                "sí responde, así que no es la API key ni el vídeo: tu conexión no llega a ese "
                "destino. Prueba con otra red (compartir los datos del móvil) o una VPN.",
                retryable=True,
            ) from failure
        if resp.status_code >= 400:
            raise ZernioError(
                "zernio_upload_failed",
                f"la subida del archivo devolvió HTTP {resp.status_code}: {resp.text[:200]}",
                retryable=500 <= resp.status_code < 600, http_status=resp.status_code,
            )

    @staticmethod
    def tiktok_settings(options: PostOptions) -> dict:
        settings: dict = {
            "privacy_level": PRIVACY_LEVEL,
            "allow_comment": not options.disable_comment,
            "allow_duet": not options.disable_duet,
            "allow_stitch": not options.disable_stitch,
            # Zernio requires both; they assert the user previewed the video and
            # consented to posting, which the UI of this tool provides.
            "content_preview_confirmed": True,
            "express_consent_given": True,
        }
        if options.brand_content_toggle:
            settings["commercialContentType"] = "brand_content"
        elif options.brand_organic_toggle:
            settings["commercialContentType"] = "brand_organic"
        if options.is_aigc:
            settings["video_made_with_ai"] = True
        return settings

    def build_post(
        self, account_id: str, media_url: str, options: PostOptions,
        scheduled_for: datetime | None = None, timezone_name: str = "",
        platform: str = "tiktok", thumbnail_url: str = "",
    ) -> dict:
        """The ``POST /posts`` body.

        ``thumbnail_url`` is the video's cover, already uploaded. On YouTube it
        is the custom thumbnail, set on the video media item: Zernio documents
        it for long videos only, not Shorts, and YouTube only takes it from a
        phone-verified channel; elsewhere the video goes out without it. On
        TikTok it is ``video_cover_image_url``.

        A TikTok post always names frame 0 as its cover too: Zernio's default
        is the frame at 1000 ms, past the 0.1 s of cover ContentBuilder puts at
        the start of its videos, so without it every cover was a random scene.
        The image wins when both are set; the frame is what a TikTok account
        that does not take cover images falls back to.

        With ``scheduled_for`` the post is handed over instead of published:
        Zernio keeps it and releases it at that instant, which is what lets the
        user close this app after sending the whole queue. ``timezone`` is sent
        alongside because Zernio reads it to interpret ``scheduledFor``; the
        timestamp itself is always explicit UTC, so the two can never disagree.
        """
        settings_dict = self.tiktok_settings(options)
        platform_data: dict = {f"{platform}Settings": settings_dict}
        if platform == "youtube":
            platform_data["visibility"] = "public"
            if options.title:
                platform_data["title"] = options.title
                settings_dict["title"] = options.title
            if options.first_comment:
                platform_data["firstComment"] = options.first_comment[:500]
            if options.tags:
                platform_data["tags"] = list(options.tags)
                settings_dict["tags"] = list(options.tags)
        else:
            if options.first_comment:
                platform_data["firstComment"] = options.first_comment[:500]
            if options.tags:
                platform_data["tags"] = list(options.tags)
            if platform == "tiktok":
                settings_dict["video_cover_timestamp_ms"] = 0
                if thumbnail_url:
                    settings_dict["video_cover_image_url"] = thumbnail_url

        video: dict = {"type": "video", "url": media_url}
        if thumbnail_url and platform == "youtube":
            video["thumbnail"] = thumbnail_url
        body = {
            "content": options.caption[:2200],
            "mediaItems": [video],
            "platforms": [{
                "platform": platform,
                "accountId": account_id,
                # Per-platform settings win over root-level ones; sending both
                # keeps us compatible with either reading of the docs.
                "platformSpecificData": platform_data,
            }],
            f"{platform}Settings": settings_dict,
        }
        if options.title:
            body["title"] = options.title
        if options.first_comment:
            body["firstComment"] = options.first_comment[:500]
        if options.tags:
            body["tags"] = list(options.tags)
        if scheduled_for is None:
            body["publishNow"] = True
        else:
            body["publishNow"] = False
            body["scheduledFor"] = (
                scheduled_for.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            )
            if timezone_name:
                body["timezone"] = timezone_name
        return body

    async def publish(
        self, account_id: str, path: Path, options: PostOptions,
        scheduled_for: datetime | None = None, timezone_name: str = "",
        platform: str = "tiktok", thumbnail: Path | None = None,
    ) -> PostResult:
        """Upload the video (and its cover, for YouTube and TikTok) and create the post.

        A cover that cannot be uploaded does not hold the video back: it goes
        out with YouTube's automatic thumbnail or TikTok's first frame, and the
        log says why.
        """
        presigned = await self.presign(path)
        await self.upload(presigned["uploadUrl"], path,
                          edge_host=urlsplit(presigned["publicUrl"]).hostname or "")
        thumbnail_url = ""
        if thumbnail is not None and platform in ("youtube", "tiktok"):
            try:
                cover = await self.presign(thumbnail)
                await self.upload(cover["uploadUrl"], thumbnail,
                                  edge_host=urlsplit(cover["publicUrl"]).hostname or "")
                thumbnail_url = cover["publicUrl"]
            except (ZernioError, OSError) as exc:
                log.warning("La portada «%s» no se pudo subir; el vídeo sale sin ella: %s",
                            thumbnail.name, exc)
        body = self.build_post(account_id, presigned["publicUrl"], options,
                               scheduled_for, timezone_name, platform=platform,
                               thumbnail_url=thumbnail_url)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(f"{self._base}/posts", headers=self._headers, json=body)
        except httpx.HTTPError as exc:
            raise ZernioError("zernio_network", f"no se pudo contactar con Zernio: {exc}",
                              retryable=True) from exc
        if resp.status_code >= 400 and resp.status_code != 207:
            self._raise(resp, "la publicación")
        return self.parse_post(_json(resp))

    async def delete_post(self, post_id: str) -> None:
        """Cancel a post Zernio still holds. A post already gone is not an error.

        TikTok cannot unpublish through any API, so this only ever removes
        something still waiting for its ``scheduledFor``.
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.delete(f"{self._base}/posts/{post_id}",
                                           headers=self._headers)
        except httpx.HTTPError as exc:
            raise ZernioError("zernio_network", f"no se pudo contactar con Zernio: {exc}",
                              retryable=True) from exc
        if resp.status_code == 404:
            return
        if resp.status_code == 400 and "published" in resp.text.lower():
            # «Published posts cannot be deleted»: its hour came before the
            # cancellation did. Callers record it as published.
            raise ZernioError("zernio_already_published",
                              "Zernio ya lo había publicado: no se puede cancelar.",
                              http_status=400)
        if resp.status_code >= 400:
            self._raise(resp, "la cancelación del post")

    async def fetch_status(self, post_id: str) -> PostResult:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{self._base}/posts/{post_id}", headers=self._headers)
        if resp.status_code >= 400:
            self._raise(resp, "la consulta de estado")
        return self.parse_post(_json(resp))

    # ------------------------------------------------------------------ #
    # Analytics
    # ------------------------------------------------------------------ #

    async def fetch_analytics(
        self, *, account_id: str | None = None, from_date: str | None = None,
        to_date: str | None = None, limit: int = 100, page: int = 1,
        platform: str = "tiktok",
    ) -> list[PostMetrics]:
        """One page of per-post numbers.

        ``fromDate`` defaults to 90 days ago on Zernio's side and the range may
        not exceed 366 days. Posts with no numbers yet come back with zeros.
        """
        params: dict[str, object] = {"platform": platform, "limit": limit, "page": page}
        if account_id:
            params["accountId"] = account_id
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(f"{self._base}/analytics", headers=self._headers,
                                        params=params)
        except httpx.HTTPError as exc:
            raise ZernioError("zernio_network", f"no se pudo contactar con Zernio: {exc}",
                              retryable=True) from exc
        if resp.status_code >= 400:
            self._raise(resp, "las analíticas")
        return self.parse_analytics(_json(resp))

    @staticmethod
    def parse_analytics(body: dict) -> list[PostMetrics]:
        """The ``posts`` array of ``GET /analytics``, as :class:`PostMetrics`.

        ``latePostId`` is read first because that is the id this tool stored
        when it created the post; ``_id``/``id`` are accepted behind it in case
        an older deployment still answers that way, and ``postId`` (the
        platform's) is the last resort, for a post Zernio synced from TikTok
        rather than published itself.
        """
        out: list[PostMetrics] = []
        for post in body.get("posts") or []:
            if not isinstance(post, dict):
                continue
            external_id = str(post.get("postId") or "")
            post_id = str(post.get("latePostId") or post.get("_id") or post.get("id") or "")
            if not (post_id or external_id):
                continue
            data = post.get("analytics") if isinstance(post.get("analytics"), dict) else {}
            out.append(PostMetrics(
                post_id=post_id or external_id,
                external_post_id=external_id,
                sync_status=str(post.get("syncStatus") or ""),
                views=_int(data.get("views")),
                likes=_int(data.get("likes")),
                comments=_int(data.get("comments")),
                shares=_int(data.get("shares")),
                profile_views=_int(data.get("profileViews")),
                completion_rate=_float(data.get("completionRate")),
                engagement_rate=_float(data.get("engagementRate")),
                updated_at=parse_dt(data.get("lastUpdated")),
            ))
        return out

    # ------------------------------------------------------------------ #
    # Comments
    # ------------------------------------------------------------------ #

    async def fetch_comments(self, post_id: str, account_id: str,
                             limit: int = 50) -> list[CommentItem]:
        """Top-level comments on one video, each with its inline replies.

        Zernio caches this for up to ten minutes, so a fresh comment can take
        that long to show up; the caller paces itself accordingly.
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{self._base}/inbox/comments/{post_id}", headers=self._headers,
                    params={"accountId": account_id, "limit": limit},
                )
        except httpx.HTTPError as exc:
            raise ZernioError("zernio_network", f"no se pudo contactar con Zernio: {exc}",
                              retryable=True) from exc
        if resp.status_code >= 400:
            self._raise(resp, "los comentarios")
        return self.parse_comments(_json(resp))

    @staticmethod
    def parse_comments(body: dict, parent: str | None = None) -> list[CommentItem]:
        """Flatten Zernio's comments, keeping the parent of each inline reply."""
        out: list[CommentItem] = []
        for raw in body.get("comments") or []:
            if not isinstance(raw, dict):
                continue
            external_id = str(raw.get("id") or "")
            if not external_id:
                continue
            author = raw.get("from") if isinstance(raw.get("from"), dict) else {}
            out.append(CommentItem(
                external_id=external_id,
                message=str(raw.get("message") or ""),
                author_name=str(author.get("name") or ""),
                author_username=str(author.get("username") or "").lstrip("@"),
                like_count=_int(raw.get("likeCount")),
                reply_count=_int(raw.get("replyCount")),
                is_owner=bool(author.get("isOwner")),
                parent_external_id=raw.get("parentId") or parent,
                created_at=parse_dt(raw.get("createdTime")),
            ))
            replies = raw.get("replies")
            if isinstance(replies, list) and replies:
                out.extend(ZernioClient.parse_comments({"comments": replies}, external_id))
        return out

    async def reply_comment(self, post_id: str, account_id: str, message: str,
                            comment_id: str | None = None) -> str | None:
        """Write a comment, or a reply when ``comment_id`` is given."""
        payload: dict[str, str] = {"accountId": account_id, "message": message}
        if comment_id:
            payload["commentId"] = comment_id
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(f"{self._base}/inbox/comments/{post_id}",
                                         headers=self._headers, json=payload)
        except httpx.HTTPError as exc:
            raise ZernioError("zernio_network", f"no se pudo contactar con Zernio: {exc}",
                              retryable=True) from exc
        if resp.status_code >= 400:
            self._raise(resp, "la respuesta al comentario")
        body = _json(resp)
        comment = body.get("comment") if isinstance(body.get("comment"), dict) else body
        return str(comment.get("id") or "") or None

    async def set_comment_hidden(self, post_id: str, comment_id: str, account_id: str,
                                 hidden: bool) -> None:
        """Hide a comment from everyone but its author, or restore it."""
        url = f"{self._base}/inbox/comments/{post_id}/{comment_id}/hide"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if hidden:
                    resp = await client.post(url, headers=self._headers,
                                             json={"accountId": account_id})
                else:
                    resp = await client.delete(url, headers=self._headers,
                                               params={"accountId": account_id})
        except httpx.HTTPError as exc:
            raise ZernioError("zernio_network", f"no se pudo contactar con Zernio: {exc}",
                              retryable=True) from exc
        if resp.status_code >= 400:
            self._raise(resp, "ocultar el comentario" if hidden else "mostrar el comentario")

    @staticmethod
    def parse_post(body: dict) -> PostResult:
        post = body.get("post") if isinstance(body.get("post"), dict) else body
        post_id = str(post.get("_id") or post.get("id") or "") or None
        status = str(post.get("status") or "").lower()
        platforms = post.get("platforms") or []
        entry = next((p for p in platforms if (p.get("platform") or "tiktok") == "tiktok"),
                     platforms[0] if platforms else {})
        p_status = str(entry.get("status") or "").lower()
        url = (entry.get("platformPostUrl") or entry.get("publishedUrl")
               or post.get("platformPostUrl") or None)
        error = (entry.get("error") or entry.get("errorMessage") or post.get("error")
                 or post.get("errorMessage") or post.get("failureReason"))

        if p_status in ("failed", "error") or status == "failed":
            return PostResult(post_id, url, "failed", str(error or "Zernio rechazó el vídeo"),
                              post)
        if status == "partial" and p_status not in ("success", "published"):
            return PostResult(post_id, url, "failed", str(error or "publicación parcial"), post)
        if status == "published" or p_status in ("success", "published"):
            # Read from both levels, as the URL is: Zernio documents
            # ``publishedAt`` on the post, and a per-platform payload repeats
            # the post envelope around the platform block.
            published_at = parse_dt(post.get("publishedAt")) or parse_dt(entry.get("publishedAt"))
            return PostResult(post_id, url, "published", "publicado a través de Zernio", post,
                              published_at=published_at)
        # scheduled / publishing / pending / queued ...
        if "schedul" in status or "schedul" in p_status:
            return PostResult(post_id, url, "scheduled", "programado en Zernio", post)
        return PostResult(post_id, url, "processing", status or p_status or "en proceso", post)


def human_hint(exc: BaseException) -> str:
    if isinstance(exc, ZernioError):
        return exc.message or exc.code
    return f"Fallo de red o del sistema hablando con Zernio: {exc}"
