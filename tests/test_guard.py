"""Requests from other web pages, other host names, and a second copy of the app."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import security
from app.db import session_scope
from app.services import instance


# --------------------------------------------------------------------------- #
# The request guard
# --------------------------------------------------------------------------- #

async def test_a_post_from_another_web_page_is_refused(client):
    for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "https://evil.example"},
                    {"Origin": "null"}, {"Sec-Fetch-Site": "same-site"}):
        resp = await client.post("/api/system/pause", json={"paused": True}, headers=headers)
        assert resp.status_code == 403, headers
        assert "otra página" in resp.json()["detail"]
    # Nothing was paused.
    assert (await client.get("/api/health")).json()["paused"] is False


async def test_the_app_itself_and_the_tools_get_through(client):
    same = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
    assert (await client.post("/api/system/pause", json={"paused": True},
                              headers=same)).status_code == 200
    # curl sends neither header.
    assert (await client.post("/api/system/pause", json={"paused": False})).status_code == 200
    # Behind a proxy that rewrites Host, the browser's name survives in X-Forwarded-Host.
    proxied = {"Origin": "https://scheduler.example", "X-Forwarded-Host": "scheduler.example"}
    assert (await client.post("/api/system/pause", json={"paused": False},
                              headers=proxied)).status_code == 200
    # Reading is never blocked by the origin check.
    assert (await client.get("/api/health", headers={"Sec-Fetch-Site": "cross-site"})
            ).status_code == 200


async def test_an_unknown_host_name_is_refused(client):
    resp = await client.get("/api/state", headers={"Host": "rebind.evil.example:8787"})
    assert resp.status_code == 400 and "ALLOWED_HOSTS" in resp.json()["detail"]


def test_which_names_are_allowed():
    loop = SimpleNamespace(host="127.0.0.1", allowed_hosts="")
    assert security.allowed_hosts(loop) == security.LOOPBACK_NAMES
    anywhere = SimpleNamespace(host="0.0.0.0", allowed_hosts="")
    assert security.allowed_hosts(anywhere) is None
    listed = SimpleNamespace(host="0.0.0.0", allowed_hosts="Scheduler.example")
    # The loopback names stay: the container's health check calls 127.0.0.1.
    assert security.allowed_hosts(listed) == security.LOOPBACK_NAMES | {"scheduler.example"}
    assert security.allowed_hosts(SimpleNamespace(host="127.0.0.1", allowed_hosts="*")) is None
    assert security._hostname("127.0.0.1:8787") == "127.0.0.1"
    assert security._hostname("[::1]:8787") == "[::1]"
    assert security._hostname("Localhost") == "localhost"


# --------------------------------------------------------------------------- #
# One copy of the app per database
# --------------------------------------------------------------------------- #

def _other(heartbeat: datetime, host: str = "otro-equipo", pid: int = 999_999) -> dict:
    return {"id": "someone-else", "host": host, "pid": pid, "port": 8787,
            "started_at": heartbeat.isoformat(), "heartbeat_at": heartbeat.isoformat()}


def _store(lease: dict) -> None:
    from app.models import Setting
    with session_scope() as session:
        row = session.get(Setting, instance.LEASE_KEY)
        if row is None:
            session.add(Setting(key=instance.LEASE_KEY, value=lease))
        else:
            row.value = lease


def test_a_second_copy_refuses_while_the_first_is_alive(fresh_db):
    now = datetime.now(timezone.utc)
    _store(_other(now - timedelta(seconds=20)))
    with session_scope() as session, pytest.raises(instance.InstanceConflict) as exc:
        instance.acquire(session, 8787, now=now)
    assert "otro-equipo" in str(exc.value) and "INSTANCE_TAKEOVER" in str(exc.value)


def test_a_stale_or_dead_lease_is_taken_over(fresh_db):
    now = datetime.now(timezone.utc)
    _store(_other(now - timedelta(seconds=instance.LEASE_TTL_SECONDS + 5)))
    with session_scope() as session:
        replaced = instance.acquire(session, 8787, now=now)
    assert replaced["id"] == "someone-else"

    # Same machine, process gone: no need to wait for the heartbeat to age.
    import socket
    _store(_other(now, host=socket.gethostname(), pid=999_999))
    with session_scope() as session:
        assert instance.acquire(session, 8787, now=now)["pid"] == 999_999

    # A container restarted as pid 1 again finds its own pid in the old lease.
    _store(_other(now, host=socket.gethostname(), pid=os.getpid()))
    with session_scope() as session:
        assert instance.acquire(session, 8787, now=now) is not None

    # And by hand, when the other one is known to be dead.
    _store(_other(now))
    with session_scope() as session:
        assert instance.acquire(session, 8787, takeover=True, now=now) is not None


def test_the_heartbeat_notices_a_takeover_and_survives_a_wipe(fresh_db):
    with session_scope() as session:
        instance.acquire(session, 8787)
    with session_scope() as session:
        assert instance.heartbeat(session, 8787) is True
        lease = instance.holder(session)
        assert lease["id"] == instance.INSTANCE_ID and instance.is_active(lease)
    # «Borrar todo» empties settings: the lease is written again.
    from app.models import Setting
    with session_scope() as session:
        session.delete(session.get(Setting, instance.LEASE_KEY))
    with session_scope() as session:
        assert instance.heartbeat(session, 8787) is True
    # Someone took it over: this copy must stop.
    _store(_other(datetime.now(timezone.utc)))
    with session_scope() as session:
        assert instance.heartbeat(session, 8787) is False
    with session_scope() as session:
        instance.release(session)  # not ours: left alone
        assert instance.holder(session)["id"] == "someone-else"


def test_the_file_copy_of_the_lease_is_enough_to_refuse(fresh_db, tmp_path):
    """Docker and the Mac may not see each other's latest rows: the file settles it."""
    marker = tmp_path / instance.MARKER
    now = datetime.now(timezone.utc)
    marker.write_text(json.dumps(_other(now - timedelta(seconds=10), host="scheduler")))
    with session_scope() as session, pytest.raises(instance.InstanceConflict, match="scheduler"):
        instance.acquire(session, 8787, now=now, marker=marker)

    # Stale: taken, and the file now says so.
    marker.write_text(json.dumps(_other(now - timedelta(seconds=instance.LEASE_TTL_SECONDS + 1))))
    with session_scope() as session:
        instance.acquire(session, 8787, now=now, marker=marker)
    assert instance.read_marker(marker)["id"] == instance.INSTANCE_ID
    with session_scope() as session:
        assert instance.heartbeat(session, 8787, marker=marker) is True

    # Another copy writes the file: this one stops on its next heartbeat.
    marker.write_text(json.dumps(_other(datetime.now(timezone.utc))))
    with session_scope() as session:
        assert instance.heartbeat(session, 8787, marker=marker) is False

    # A clean shutdown removes only its own file.
    with session_scope() as session:
        instance.release(session, marker)
    assert marker.exists()
    with session_scope() as session:
        instance.acquire(session, 8787, takeover=True, marker=marker)
        instance.release(session, marker)
    assert not marker.exists()
