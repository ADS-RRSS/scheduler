"""Moving to a new Mac: the snapshot carries the database, the key, .env and the bank."""
from __future__ import annotations

import io
import json
import sqlite3
import stat
import tarfile
from pathlib import Path

import pytest

from app.config import get_settings
from tests.helpers import make_account
from tools import snapshot


def _paths(root: Path, db: Path) -> snapshot.Paths:
    return snapshot.Paths(database=db, key=root / "secrets" / "encryption.key",
                          env=root / ".env", bank=root / "hashtags.json",
                          backups=root / "backups", port=1)


async def test_a_snapshot_goes_to_another_machine_and_back(client, tmp_path):
    await make_account(client)  # an account, so there is an encrypted key worth carrying
    source = tmp_path / "old-mac"
    (source / "secrets").mkdir(parents=True)
    (source / "secrets" / "encryption.key").write_bytes(b"the-key")
    (source / ".env").write_text("PORT=8787\nOLLAMA_MODEL=gemma4:26b\n")
    (source / "hashtags.json").write_text('{"categories": {}}')
    old = _paths(source, get_settings().database_path)

    archive = snapshot.create(old)
    assert archive.name.startswith("snapshot-") and archive.suffixes[-2:] == [".tar", ".gz"]
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600, "lleva la clave: sólo para su dueño"
    manifest, files = snapshot.read(archive)
    assert set(files) == {"scheduler.db", "encryption.key", "env", "hashtags.json"}
    assert manifest["counts"]["accounts"] == 1

    # The new Mac already has a half-made install: it is saved before being replaced.
    target = tmp_path / "new-mac"
    (target / "data").mkdir(parents=True)
    new_db = target / "data" / "scheduler.db"
    sqlite3.connect(new_db).execute("CREATE TABLE settings (key TEXT, value TEXT)").connection.commit()
    Path(str(new_db) + "-wal").write_bytes(b"stale wal")
    (target / ".env").write_text("PORT=9999\n")
    new = _paths(target, new_db)
    done = snapshot.restore(archive, new)

    assert not Path(str(new_db) + "-wal").exists(), "un -wal viejo se aplicaría a la base nueva"
    conn = sqlite3.connect(new_db)
    assert conn.execute("SELECT count(*) FROM accounts").fetchone()[0] == 1
    conn.close()
    assert new.key.read_bytes() == b"the-key" and stat.S_IMODE(new.key.stat().st_mode) == 0o600
    assert "OLLAMA_MODEL=gemma4:26b" in new.env.read_text()
    assert (target / ".env.before-restore").read_text() == "PORT=9999\n"
    assert new.bank.read_text() == '{"categories": {}}'
    assert any("pre-restore" in line for line in done)
    assert list((target / "backups").glob("pre-restore-*.tar.gz"))


def test_a_damaged_or_foreign_archive_is_refused(tmp_path):
    bad = tmp_path / "x.tar.gz"
    with tarfile.open(bad, "w:gz") as tar:
        info = tarfile.TarInfo("../../etc/passwd")
        data = b"nope"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    with pytest.raises(SystemExit):
        snapshot.read(bad)

    manifest = {"files": {"scheduler.db": {"sha256": "0" * 64}}}
    forged = tmp_path / "forged.tar.gz"
    with tarfile.open(forged, "w:gz") as tar:
        for name, data in (("MANIFEST.json", json.dumps(manifest).encode()), ("scheduler.db", b"x")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    with pytest.raises(SystemExit, match="dañado"):
        snapshot.read(forged)


def test_restore_refuses_while_the_app_holds_the_database(fresh_db, tmp_path):
    from app.db import session_scope
    from app.services import instance

    db = get_settings().database_path
    with session_scope() as session:
        instance.acquire(session, 8787)
    archive = snapshot.create(_paths(tmp_path, db))
    with pytest.raises(SystemExit, match="en uso"):
        snapshot.restore(archive, _paths(tmp_path, db))
