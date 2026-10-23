#!/usr/bin/env python3
"""Everything a new Mac needs to carry on where this one left off, in one file.

    make snapshot                        (tools/snapshot.py create)
    make restore FILE=data/backups/snapshot-….tar.gz
    tools/snapshot.py list

A snapshot holds what cannot be rebuilt:

* the database, copied with SQLite's online backup API (consistent even with
  the app open; a plain copy of ``scheduler.db`` without its ``-wal`` loses
  the latest writes, and with a stale one it can come back corrupt);
* ``.secrets/encryption.key``, without which every API key stored in that
  database is unreadable;
* ``.env`` and the hashtag bank (``data/hashtags.json``), which you edit.

The videos are not in it: they are tens of gigabytes and are copied as they
are (``rsync -a media/ otro-mac:…/media/``, or a disk). The Whisper weights are
downloaded again (``make models``).

The file holds the encryption key: keep it like a password (it is written
with permissions 600). Restoring refuses while the app is running, and saves
what it is about to replace first.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import shutil
import socket
import sqlite3
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.config import APP_VERSION, get_settings  # noqa: E402
from app.services import backups, instance  # noqa: E402

MANIFEST = "MANIFEST.json"
# Names inside the archive, whatever the paths on either machine.
DB, KEY, ENV, BANK = "scheduler.db", "encryption.key", "env", "hashtags.json"


@dataclass
class Paths:
    """Where each piece lives on this machine."""

    database: Path
    key: Path
    env: Path
    bank: Path
    backups: Path
    port: int

    @classmethod
    def current(cls) -> "Paths":
        s = get_settings()
        if s.database_path is None:
            raise SystemExit("La base de datos no es un archivo SQLite local: no hay nada que copiar.")
        return cls(database=s.database_path, key=Path(s.secrets_dir) / "encryption.key",
                   env=BASE / ".env", bank=Path(s.hashtag_bank_file),
                   backups=Path(s.backups_dir), port=s.port)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _counts(db: Path) -> dict:
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            out = {"accounts": conn.execute("SELECT count(*) FROM accounts").fetchone()[0]}
            out.update(dict(conn.execute(
                "SELECT lower(status), count(*) FROM video_items GROUP BY status").fetchall()))
            version = conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
            out["schema_version"] = json.loads(version[0]).get("value") if version else None
            return out
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


def create(paths: Paths | None = None, *, label: str = "snapshot") -> Path:
    """Write ``<backups>/<label>-YYYYmmdd-HHMMSS.tar.gz``. Returns its path."""
    paths = paths or Paths.current()
    if not paths.database.exists():
        raise SystemExit(f"No hay base de datos en {paths.database}: nada que guardar todavía.")
    files: dict[str, bytes] = {}
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / DB
        backups.copy_database(paths.database, copy)
        files[DB] = copy.read_bytes()
    for name, path in ((KEY, paths.key), (ENV, paths.env), (BANK, paths.bank)):
        if path.exists():
            files[name] = path.read_bytes()
    manifest = {
        "app_version": APP_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "counts": _counts(paths.database),
        "files": {name: {"sha256": _sha256(data), "bytes": len(data)} for name, data in files.items()},
    }
    paths.backups.mkdir(parents=True, exist_ok=True)
    target = paths.backups / f"{label}-{datetime.now():%Y%m%d-%H%M%S}.tar.gz"
    # Created 600 from the start: it holds the key.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, data in [(MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False).encode()),
                           *files.items()]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            info.mtime = int(datetime.now().timestamp())
            tar.addfile(info, io.BytesIO(data))
    return target


def read(archive: Path) -> tuple[dict, dict[str, bytes]]:
    """The manifest and the files of a snapshot, checked against their hashes."""
    files: dict[str, bytes] = {}
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            # Plain files with plain names: nothing else is ever written here.
            if not member.isfile() or "/" in member.name or member.name.startswith("."):
                raise SystemExit(f"{archive.name} no es una instantánea de esta app ({member.name}).")
            files[member.name] = tar.extractfile(member).read()
    if MANIFEST not in files or DB not in files:
        raise SystemExit(f"{archive.name} no es una instantánea de esta app (falta {MANIFEST} o {DB}).")
    manifest = json.loads(files.pop(MANIFEST))
    for name, data in files.items():
        expected = manifest.get("files", {}).get(name, {}).get("sha256")
        if expected != _sha256(data):
            raise SystemExit(f"{archive.name}: «{name}» está dañado (no coincide su suma).")
    return manifest, files


def app_running(paths: Paths) -> str | None:
    """Why restoring now would be unsafe, or None."""
    with socket.socket() as sock:
        sock.settimeout(0.5)
        if sock.connect_ex(("127.0.0.1", paths.port)) == 0:
            return f"algo escucha en el puerto {paths.port} (¿la app está abierta?)"
    if paths.database.exists():
        try:
            conn = sqlite3.connect(f"file:{paths.database}?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT value FROM settings WHERE key=?",
                                   (instance.LEASE_KEY,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            row = None
        lease = json.loads(row[0]) if row and isinstance(row[0], str) else None
        if lease and instance.is_active(lease):
            return f"la base de datos está en uso por {instance.describe(lease)}"
    on_file = instance.read_marker(instance.marker_path(paths.database))
    if on_file and instance.is_active(on_file):
        return f"la base de datos está en uso por {instance.describe(on_file)}"
    return None


def restore(archive: Path, paths: Paths | None = None, *, force: bool = False) -> list[str]:
    """Put a snapshot in place. Returns what was done, line by line."""
    paths = paths or Paths.current()
    manifest, files = read(archive)
    busy = app_running(paths)
    if busy and not force:
        raise SystemExit(f"No se restaura: {busy}. Cierra la app (y Docker) y vuelve a intentarlo.")
    done: list[str] = []
    if paths.database.exists():
        saved = create(paths, label="pre-restore")
        done.append(f"Lo que había se guardó antes en {saved}")
    # The database: its old -wal/-shm would be replayed onto the new file.
    paths.database.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(paths.database) + suffix).unlink(missing_ok=True)
    tmp = paths.database.with_name(paths.database.name + ".restoring")
    tmp.write_bytes(files[DB])
    os.replace(tmp, paths.database)
    done.append(f"Base de datos → {paths.database} ({manifest.get('counts', {})})")
    if KEY in files:
        paths.key.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(paths.key.parent, 0o700)
        if paths.key.exists() and paths.key.read_bytes() != files[KEY]:
            shutil.copy2(paths.key, paths.key.with_name("encryption.key.before-restore"))
        paths.key.write_bytes(files[KEY])
        os.chmod(paths.key, 0o600)
        done.append(f"Clave de cifrado → {paths.key}")
    else:
        done.append("⚠ La instantánea no traía clave de cifrado: las API keys habrá que pegarlas otra vez")
    if ENV in files:
        if paths.env.exists() and paths.env.read_bytes() != files[ENV]:
            shutil.copy2(paths.env, paths.env.with_name(".env.before-restore"))
            done.append("Tu .env anterior se guardó como .env.before-restore")
        paths.env.write_bytes(files[ENV])
        done.append(f".env → {paths.env}")
        for line in files[ENV].decode("utf-8", "replace").splitlines():
            key, _, value = line.partition("=")
            if key.strip() in ("DATABASE_URL", "SECRETS_DIR", "BACKUPS_DIR", "MEDIA_LIBRARY_DIR") \
                    and value.strip() and not line.lstrip().startswith("#"):
                done.append(f"⚠ El .env restaurado fija {key.strip()}={value.strip()}: revisa que esa "
                            "ruta exista en este equipo")
    if BANK in files:
        paths.bank.parent.mkdir(parents=True, exist_ok=True)
        paths.bank.write_bytes(files[BANK])
        done.append(f"Banco de hashtags → {paths.bank}")
    return done


def list_snapshots(paths: Paths | None = None) -> list[Path]:
    paths = paths or Paths.current()
    return sorted(paths.backups.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if paths.backups.exists() else []


def main() -> int:
    parser = argparse.ArgumentParser(description="Instantáneas para llevarse la app a otro equipo.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create", help="crea una instantánea en data/backups")
    rest = sub.add_parser("restore", help="restaura una instantánea (con la app cerrada)")
    rest.add_argument("file", type=Path)
    rest.add_argument("--force", action="store_true", help="aunque la app parezca abierta")
    sub.add_parser("list", help="las instantáneas que hay")
    args = parser.parse_args()

    if args.command == "create":
        target = create()
        manifest, _ = read(target)
        print(f"✓ Instantánea: {target} ({target.stat().st_size / 1024 ** 2:.1f} MB)")
        print(f"  {manifest['counts']}")
        print("  Contiene la clave de cifrado: guárdala fuera de este Mac y como una contraseña.")
        print("  Los vídeos no van dentro: copia media/ aparte (rsync -a media/ destino:…/media/).")
        return 0
    if args.command == "restore":
        for line in restore(args.file, force=args.force):
            print(f"✓ {line}" if not line.startswith("⚠") else line)
        print("\nSiguiente: copia media/ si aún no está, luego  make doctor  y  make run")
        return 0
    for path in list_snapshots():
        print(f"{path.name}  {path.stat().st_size / 1024 ** 2:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
