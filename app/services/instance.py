"""One scheduler per database.

Two copies of the app on the same ``scheduler.db`` would both publish: the
same video twice on TikTok, and two workers fighting over the same rows.
With a native install and a Docker image sharing ``data/`` that is one
forgotten terminal away, and a second ``./run.sh`` on another port is enough.

So the app takes a lease when it starts: a row in ``settings`` with who holds
the database (a random id per process, the host name, the pid) and a
heartbeat renewed every :data:`HEARTBEAT_SECONDS` by a task of its own — not
by the worker's tick, which can spend minutes inside one upload. Another copy
refuses to start while that heartbeat is fresh.

The same lease is also written to a small file next to the database
(:data:`MARKER`). A copy in Docker and a copy on the Mac read the one SQLite
file through two different kernels, and WAL's shared-memory index is not
shared between them: the other side may read an old version of the rows, the
lease included. A file replaced with an atomic rename is seen by both.

A lease left behind by a crash does not lock anyone out for long: it goes
stale after :data:`LEASE_TTL_SECONDS`, and on the same machine it is taken
over at once when its process is gone. ``INSTANCE_TAKEOVER=true`` takes it
regardless, for when the other copy is known to be dead; the copy that loses
its lease that way stops publishing on its next heartbeat.
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Setting

LEASE_KEY = "instance_lease"
MARKER = ".scheduler-instance.json"
HEARTBEAT_SECONDS = 30
LEASE_TTL_SECONDS = 90
# This process, for as long as it lives.
INSTANCE_ID = uuid.uuid4().hex


class InstanceConflict(RuntimeError):
    """Another copy of the app holds this database."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    return True


def holder(session: Session) -> dict | None:
    """The lease as stored, whoever holds it."""
    row = session.get(Setting, LEASE_KEY)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else None


def marker_path(database_path: Path | None) -> Path | None:
    """Where the file copy of the lease lives: next to the SQLite file."""
    return Path(database_path).parent / MARKER if database_path else None


def read_marker(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_marker(path: Path | None, lease: dict) -> None:
    if path is None:
        return
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(lease), encoding="utf-8")
    os.replace(tmp, path)


def _someone_else(session: Session, marker: Path | None) -> dict | None:
    """The other copy's lease, from the database or from the file, the fresher of the two."""
    found = [lease for lease in (holder(session), read_marker(marker))
             if lease and lease.get("id") != INSTANCE_ID]
    if not found:
        return None
    return min(found, key=lambda lease: age_seconds(lease) if age_seconds(lease) is not None
               else float("inf"))


def age_seconds(lease: dict | None, now: datetime | None = None) -> float | None:
    beat = _parse((lease or {}).get("heartbeat_at"))
    return ((now or _now()) - beat).total_seconds() if beat else None


def is_active(lease: dict | None, now: datetime | None = None) -> bool:
    """Is someone (maybe this process) holding the database right now?"""
    age = age_seconds(lease, now)
    return age is not None and age <= LEASE_TTL_SECONDS


def describe(lease: dict) -> str:
    where = lease.get("host") or "otro equipo"
    since = str(lease.get("started_at") or "?")[:19].replace("T", " ")
    return f"{where} (pid {lease.get('pid', '?')}, puerto {lease.get('port', '?')}, desde {since} UTC)"


def _write(session: Session, value: dict) -> None:
    row = session.get(Setting, LEASE_KEY)
    if row is None:
        session.add(Setting(key=LEASE_KEY, value=value))
    else:
        row.value = value


def acquire(session: Session, port: int, *, takeover: bool = False,
            now: datetime | None = None, marker: Path | None = None) -> dict | None:
    """Take the database for this process. Returns the lease it replaced, if any.

    Raises :class:`InstanceConflict` when another live copy holds it.
    """
    now = now or _now()
    lease = _someone_else(session, marker) or holder(session)
    replaced = None
    if lease and lease.get("id") != INSTANCE_ID:
        same_host = lease.get("host") == socket.gethostname()
        # Same machine: a pid that is gone (or that is ours: a container
        # restarted as pid 1 again) cannot be publishing.
        gone = same_host and (lease.get("pid") == os.getpid() or not pid_alive(lease.get("pid")))
        if is_active(lease, now) and not gone and not takeover:
            raise InstanceConflict(
                "Otra copia de Scheduler está usando esta base de datos: "
                f"{describe(lease)}. Ciérrala antes de abrir esta (dos copias publicarían "
                f"cada vídeo dos veces). Si sabes que ya no existe, espera "
                f"{LEASE_TTL_SECONDS} s o arranca con INSTANCE_TAKEOVER=true.")
        replaced = lease
    ours = lease is not None and lease.get("id") == INSTANCE_ID
    started = lease.get("started_at") if ours else now.isoformat()
    value = {
        "id": INSTANCE_ID,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "port": port,
        "started_at": started,
        "heartbeat_at": now.isoformat(),
    }
    _write(session, value)
    _write_marker(marker, value)
    return replaced


def heartbeat(session: Session, port: int, now: datetime | None = None,
              marker: Path | None = None) -> bool:
    """Renew the lease. False when another copy has taken it meanwhile.

    A lease that is simply gone («Borrar todo» empties ``settings`` too) is
    written again: nobody else has claimed the database.
    """
    lease = holder(session)
    if lease and lease.get("id") != INSTANCE_ID:
        return False  # taken over, as the database says
    on_file = read_marker(marker)
    if on_file and on_file.get("id") != INSTANCE_ID and is_active(on_file, now):
        return False  # taken over, as the file says (the database may lag behind)
    if lease is None:
        acquire(session, port, now=now, marker=marker)
        return True
    lease["heartbeat_at"] = (now or _now()).isoformat()
    _write(session, lease)
    _write_marker(marker, lease)
    return True


def release(session: Session, marker: Path | None = None) -> None:
    """Give the database back on a clean shutdown (only if it is still ours)."""
    lease = holder(session)
    if lease and lease.get("id") == INSTANCE_ID:
        row = session.get(Setting, LEASE_KEY)
        session.delete(row)
    on_file = read_marker(marker)
    if marker is not None and on_file and on_file.get("id") == INSTANCE_ID:
        marker.unlink(missing_ok=True)
