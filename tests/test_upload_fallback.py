"""The upload's way out when the network cannot reach the presigned host.

The bytes do not go to zernio.com: presign hands out a URL on Cloudflare R2,
on a different IP prefix. When an ISP blackholes that prefix the API keeps
answering and only the upload dies, so the client re-routes the PUT through an
address of the host Zernio itself pointed at (``publicUrl``), with the Host
header and the SNI still on the original name - the S3 signature covers Host
and the certificate is checked against the name, not the address.
"""
from __future__ import annotations

import httpx
import pytest

from app.services import zernio

BLOCKED = "late-media.abc123.r2.cloudflarestorage.com"
UPLOAD_URL = f"https://{BLOCKED}/temp/1789_reel_001.mp4?X-Amz-Signature=deadbeef"
EDGE = "104.21.54.89"


@pytest.fixture(autouse=True)
def forget_learnt_routes():
    zernio._edge_routes.clear()
    yield
    zernio._edge_routes.clear()


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "reel_001.mp4"
    path.write_bytes(b"videobytes" * 100)
    return path


def use_transport(monkeypatch, handler):
    """Make every client the module opens answer through ``handler``."""
    real = httpx.AsyncClient
    monkeypatch.setattr(
        zernio.httpx, "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs),
    )


def blocked_prefix(requests: list[httpx.Request]):
    """A network where only the blocked host's own address refuses to connect."""
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == BLOCKED:
            raise httpx.ConnectError("All connection attempts failed")
        return httpx.Response(200)
    return handler


async def test_upload_goes_straight_out_when_the_network_is_fine(monkeypatch, video):
    seen: list[httpx.Request] = []
    use_transport(monkeypatch, lambda request: (seen.append(request), httpx.Response(200))[1])

    await zernio.ZernioClient("key").upload(UPLOAD_URL, video, edge_host="media.zernio.com")

    assert [str(r.url) for r in seen] == [UPLOAD_URL]
    assert "sni_hostname" not in seen[0].extensions
    assert seen[0].content == video.read_bytes()


async def test_a_blocked_prefix_is_worked_around_through_the_other_host(monkeypatch, video):
    seen: list[httpx.Request] = []
    use_transport(monkeypatch, blocked_prefix(seen))
    monkeypatch.setattr(zernio, "_addresses", lambda host: _addresses_of(host))

    await zernio.ZernioClient("key").upload(UPLOAD_URL, video, edge_host="media.zernio.com")

    assert len(seen) == 2, "the direct attempt first, then the way around"
    rerouted = seen[1]
    assert str(rerouted.url) == f"https://{EDGE}/temp/1789_reel_001.mp4?X-Amz-Signature=deadbeef"
    # Host is part of what the presigned URL signed, and the certificate is
    # checked against the name: only the route changes.
    assert rerouted.headers["Host"] == BLOCKED
    assert rerouted.extensions["sni_hostname"] == BLOCKED
    assert rerouted.headers["Content-Length"] == str(video.stat().st_size)
    assert rerouted.content == video.read_bytes()


async def test_the_route_that_worked_is_reused_by_the_next_video(monkeypatch, video):
    seen: list[httpx.Request] = []
    use_transport(monkeypatch, blocked_prefix(seen))
    monkeypatch.setattr(zernio, "_addresses", lambda host: _addresses_of(host))
    client = zernio.ZernioClient("key")

    await client.upload(UPLOAD_URL, video, edge_host="media.zernio.com")
    seen.clear()
    await client.upload(UPLOAD_URL, video, edge_host="media.zernio.com")

    # No second connect timeout: the queue does not pay for the block again.
    assert [r.url.host for r in seen] == [EDGE]


async def test_nothing_reachable_is_explained_and_retried_later(monkeypatch, video):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed")

    use_transport(monkeypatch, handler)
    monkeypatch.setattr(zernio, "_addresses", lambda host: _addresses_of(host))

    with pytest.raises(zernio.ZernioError) as caught:
        await zernio.ZernioClient("key").upload(UPLOAD_URL, video, edge_host="media.zernio.com")

    assert caught.value.code == "zernio_upload_unreachable"
    assert caught.value.retryable
    assert BLOCKED in caught.value.message


async def _addresses_of(host: str) -> list[str]:
    return [EDGE] if host == "media.zernio.com" else []
