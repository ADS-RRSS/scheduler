"""The database copies itself once a day, and only the automatic copies rotate."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.config import get_settings
from app.services import backups
from tests.helpers import make_account, tick


@pytest.fixture
def auto_on():
    s = get_settings()
    saved = (s.backup_auto_hours, s.backup_keep)
    s.backup_auto_hours, s.backup_keep = 24, 3
    yield s
    s.backup_auto_hours, s.backup_keep = saved


def _age(path: Path, hours: float) -> None:
    then = time.time() - hours * 3600
    os.utime(path, (then, then))


async def test_the_worker_copies_the_database_once_a_day(client, auto_on):
    await make_account(client)
    folder = Path(auto_on.backups_dir)
    await tick()
    first = [r for r in backups.list_all(folder) if r["auto"]]
    assert len(first) == 1 and first[0]["name"].startswith(backups.AUTO_PREFIX)
    await tick()
    assert len([r for r in backups.list_all(folder) if r["auto"]]) == 1, "no otra en el mismo día"

    # A day later, another one.
    _age(folder / first[0]["name"], 25)
    time.sleep(1.1)  # the name carries the second
    await tick()
    assert len([r for r in backups.list_all(folder) if r["auto"]]) == 2


def test_only_the_automatic_copies_rotate(auto_on, fresh_db):
    folder = Path(auto_on.backups_dir)
    manual, _ = backups.create(auto_on)
    names = []
    for hours in (100, 80, 60, 40, 20):
        name, _ = backups.create(auto_on, auto=True)
        _age(folder / name, hours)
        names.append(name)
        time.sleep(1.05)
    removed = backups.prune_auto(auto_on.backup_keep, folder)
    left = {r["name"] for r in backups.list_all(folder)}
    assert removed == 2
    assert manual in left, "una copia hecha a mano no se borra nunca sola"
    assert set(names[-3:]) <= left and not set(names[:2]) & left


def test_no_automatic_copy_when_turned_off(fresh_db):
    s = get_settings()
    assert s.backup_auto_hours == 0
    assert backups.run_auto(s) is None


async def test_automatic_copies_are_listed_and_downloadable(client, auto_on):
    name, _ = backups.create(auto_on, auto=True)
    listed = (await client.get("/api/admin/backups")).json()["backups"]
    assert any(b["name"] == name and b["auto"] for b in listed)
    resp = await client.get(f"/api/admin/backups/{name}")
    assert resp.status_code == 200 and resp.content[:15] == b"SQLite format 3"
    system = (await client.get("/api/admin/system")).json()["backups"]
    assert system["auto_hours"] == 24 and system["keep"] == 3
    assert (await client.delete(f"/api/admin/backups/{name}")).status_code == 200
    assert (await client.get("/api/admin/backups/scheduler-auto-x.db")).status_code == 400
