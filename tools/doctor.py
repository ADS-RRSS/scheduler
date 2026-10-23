#!/usr/bin/env python3
"""¿Está todo listo para que Scheduler funcione en este equipo?

    make doctor                 (o  python3 tools/doctor.py)
    python3 tools/doctor.py --offline   sin comprobar la red

Sólo usa la biblioteca estándar, así que funciona aunque el entorno virtual no
exista o esté roto: es lo primero que hay que mirar en un Mac nuevo, después
de una actualización de macOS o de brew, o cuando «no publica».

Cada línea es ✓ (bien), ⚠ (funciona, pero conviene mirarlo) o ✗ (no va a
funcionar así). Sale con código 1 si hay alguna ✗.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OK, WARN, FAIL = "✓", "⚠", "✗"
LEASE_TTL_SECONDS = 90  # app/services/instance.py
RECOMMENDED_MODEL = "gemma4:26b"


class Report:
    def __init__(self) -> None:
        self.failed = False
        self.warned = False

    def section(self, title: str) -> None:
        print(f"\n{title}")

    def line(self, mark: str, text: str, hint: str = "") -> None:
        self.failed |= mark == FAIL
        self.warned |= mark == WARN
        print(f"  {mark} {text}")
        if hint:
            print(f"      → {hint}")


def read_env(path: Path) -> dict[str, str]:
    """KEY=value lines of .env, without running anything."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip().upper()] = value.strip().strip('"').strip("'")
    return values


def setting(env: dict[str, str], key: str, default: str) -> str:
    return os.environ.get(key, env.get(key, default)) or default


def run(cmd: list[str], timeout: float = 10) -> tuple[int, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.returncode, (out.stdout or out.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def _ssl_context():
    """The CA bundle the app itself uses (certifi, via httpx).

    The python.org installer's Python ships without root certificates until
    «Install Certificates.command» is run: urllib fails every HTTPS request
    while the app, on certifi, works. The check must see what the app sees.
    """
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    for bundle in sorted(BASE.glob(".venv/lib/python3*/site-packages/certifi/cacert.pem")):
        return ssl.create_default_context(cafile=str(bundle))
    return ssl.create_default_context()


def http_status(url: str, timeout: float = 6, method: str = "GET") -> int | str:
    request = urllib.request.Request(url, method=method,
                                     headers={"User-Agent": "scheduler-doctor"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=_ssl_context()) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception as exc:  # noqa: BLE001 - what failed is the answer
        return f"{type(exc).__name__}: {exc}"[:120]


def gb(n: float) -> str:
    return f"{n / 1024 ** 3:.1f} GB"


# --------------------------------------------------------------------------- #

def check_python(r: Report) -> Path | None:
    r.section("Python")
    here = sys.version_info
    r.line(OK if here >= (3, 11) else WARN,
           f"Este diagnóstico corre con Python {platform.python_version()} ({sys.executable})")
    venv = BASE / ".venv" / "bin" / "python"
    if not venv.exists():
        r.line(FAIL, "No hay entorno virtual (.venv)", "make setup   (o ./run.sh --setup)")
        return None
    code, version = run([str(venv), "-c", "import platform; print(platform.python_version())"])
    if code != 0:
        r.line(FAIL, "El entorno virtual está roto (su Python ya no existe)",
               "rm -rf .venv && make setup")
        return None
    major, minor = (int(x) for x in version.split(".")[:2])
    if (major, minor) < (3, 11):
        r.line(FAIL, f"El entorno usa Python {version}; hace falta 3.11 o superior",
               "brew install python@3.13 && rm -rf .venv && make setup")
        return None
    r.line(OK, f"Entorno virtual con Python {version}")
    code, out = run([str(venv), "-c",
                     "import fastapi, sqlalchemy, httpx, pydantic_settings, cryptography; print('ok')"])
    r.line(OK if code == 0 else FAIL, "Dependencias de la app" + ("" if code == 0 else f": {out[-160:]}"),
           "" if code == 0 else "./.venv/bin/python -m pip install -r requirements.txt")
    code, out = run([str(venv), "-c", "import faster_whisper; print(faster_whisper.__version__)"])
    r.line(OK if code == 0 else WARN,
           f"faster-whisper {out}" if code == 0 else "faster-whisper no se puede importar",
           "" if code == 0 else "sin él no hay transcripción: pip install -r requirements.txt")
    return venv


def check_tools(r: Report) -> None:
    r.section("Herramientas")
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        if path:
            _, out = run([tool, "-version"])
            r.line(OK, f"{tool}: {out.splitlines()[0][:70] if out else path}")
        else:
            r.line(WARN, f"{tool} no está instalado", "brew install ffmpeg (sin él no se valida nada antes de subir)")
    brew = shutil.which("brew")
    r.line(OK if brew else WARN, "Homebrew" + (f" en {brew}" if brew else " no está"),
           "" if brew else "https://brew.sh (o instala Python y ffmpeg a mano)")
    docker = shutil.which("docker")
    if docker:
        code, out = run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=6)
        r.line(OK if code == 0 else WARN,
               f"Docker {out}" if code == 0 else "Docker instalado pero sin motor en marcha (opcional)",
               "" if code == 0 else "abre OrbStack o Docker Desktop si vas a usar make docker-up")
    else:
        r.line(OK, "Docker no está instalado (opcional: sólo para make docker-up)")


def check_config(r: Report, env: dict[str, str]) -> None:
    r.section("Configuración")
    env_file = BASE / ".env"
    r.line(OK if env_file.exists() else WARN, ".env" + ("" if env_file.exists() else " no existe"),
           "" if env_file.exists() else "make setup lo crea desde .env.example")
    for key in ("DATABASE_URL", "SECRETS_DIR", "BACKUPS_DIR", "MEDIA_LIBRARY_DIR"):
        value = env.get(key)
        if not value:
            continue
        target = Path(value.replace("sqlite:///", "")) if key == "DATABASE_URL" else Path(value)
        parent = target.parent if key == "DATABASE_URL" else target
        r.line(OK if parent.exists() else FAIL, f"{key} → {value}",
               "" if parent.exists() else "esa ruta no existe en este equipo (¿.env de otro Mac?)")
    host = setting(env, "HOST", "127.0.0.1")
    passcode = setting(env, "UI_PASSCODE", "")
    if host not in ("127.0.0.1", "localhost", "::1"):
        r.line(WARN if not passcode else OK, f"HOST={host}: la app se ve desde otros equipos",
               "" if passcode else "pon UI_PASSCODE y ALLOWED_HOSTS en .env")
    else:
        r.line(OK, f"Sólo en este equipo (HOST={host})"
               + ("" if passcode else " · sin código de acceso"))


def check_secrets(r: Report, env: dict[str, str], accounts: int) -> None:
    secrets_dir = Path(setting(env, "SECRETS_DIR", str(BASE / ".secrets")))
    key = secrets_dir / "encryption.key"
    if setting(env, "APP_ENCRYPTION_KEY", ""):
        r.line(OK, "Clave de cifrado en APP_ENCRYPTION_KEY")
        return
    if not key.exists():
        if accounts:
            r.line(FAIL, f"Falta {key} y hay {accounts} cuenta(s): sus API keys no se pueden leer",
                   "restaura la clave del otro Mac (make restore FILE=…) o vuelve a pegar las claves en Cuentas")
        else:
            r.line(OK, "La clave de cifrado se creará al conectar la primera cuenta")
        return
    mode = key.stat().st_mode & 0o777
    r.line(OK if mode & 0o077 == 0 else WARN, f"Clave de cifrado {key} (permisos {oct(mode)[2:]})",
           "" if mode & 0o077 == 0 else f"chmod 600 {key}")
    backups = Path(setting(env, "BACKUPS_DIR", str(BASE / "data" / "backups")))
    snapshots = sorted(backups.glob("snapshot-*.tar.gz"), key=lambda p: p.stat().st_mtime) \
        if backups.exists() else []
    if snapshots:
        age_d = (datetime.now().timestamp() - snapshots[-1].stat().st_mtime) / 86400
        r.line(OK if age_d < 30 else WARN,
               f"Última instantánea (base de datos + clave + .env) hace {age_d:.0f} día(s): {snapshots[-1].name}",
               "" if age_d < 30 else "make snapshot, y guárdala fuera de este Mac")
    elif accounts:
        r.line(WARN, "Sin esta clave las API keys guardadas no sirven y no hay ninguna instantánea",
               "make snapshot, y guarda el archivo fuera de este Mac (como una contraseña)")


def check_database(r: Report, env: dict[str, str]) -> int:
    r.section("Base de datos")
    url = setting(env, "DATABASE_URL", f"sqlite:///{BASE / 'data' / 'scheduler.db'}")
    path = Path(url.replace("sqlite:///", ""))
    if not path.exists():
        r.line(OK, f"Aún no existe ({path}): se crea al arrancar")
        return 0
    size = sum(Path(str(path) + s).stat().st_size for s in ("", "-wal", "-shm") if Path(str(path) + s).exists())
    accounts = 0
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            quick = conn.execute("PRAGMA quick_check").fetchone()[0]
            version = conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
            accounts = conn.execute("SELECT count(*) FROM accounts").fetchone()[0]
            counts = dict(conn.execute("SELECT status, count(*) FROM video_items GROUP BY status").fetchall())
            library = conn.execute("SELECT count(*) FROM library_items").fetchone()[0] \
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='library_items'").fetchone() else 0
            lease = conn.execute("SELECT value FROM settings WHERE key='instance_lease'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        r.line(FAIL, f"No se puede leer {path}: {exc}", "restaura una copia de data/backups")
        return 0
    schema = json.loads(version[0]).get("value") if version else "?"
    r.line(OK if quick == "ok" else FAIL, f"{path} · {size / 1024 ** 2:.1f} MB · esquema v{schema} · integridad {quick}")
    r.line(OK, f"{accounts} cuenta(s) · {library} vídeo(s) en la biblioteca · "
           + (", ".join(f"{k.lower()} {v}" for k, v in sorted(counts.items())) or "ninguno en colas"))
    if counts.get("DELEGATED"):
        r.line(OK, f"{counts['DELEGATED']} vídeo(s) entregados a Zernio: esta base de datos es su único registro aquí")
    if lease:
        value = json.loads(lease[0]) if isinstance(lease[0], str) else {}
        beat = value.get("heartbeat_at")
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(beat)).total_seconds() if beat else None
        if age is not None and age <= LEASE_TTL_SECONDS:
            r.line(OK, f"La app está en marcha y usa esta base de datos ({value.get('host')}, pid {value.get('pid')}, "
                   f"puerto {value.get('port')})")
    backups = Path(setting(env, "BACKUPS_DIR", str(BASE / "data" / "backups")))
    copies = sorted(backups.glob("scheduler-*.db"), key=lambda p: p.stat().st_mtime) if backups.exists() else []
    if copies:
        age_h = (datetime.now().timestamp() - copies[-1].stat().st_mtime) / 3600
        r.line(OK if age_h < 48 else WARN, f"{len(copies)} copia(s); la última hace {age_h:.0f} h ({copies[-1].name})",
               "" if age_h < 48 else "la app hace una al día mientras está abierta; o  make backup")
    else:
        r.line(WARN, "Ninguna copia de la base de datos todavía", "make backup (y la app hará una al día)")
    return accounts


def check_disk(r: Report, env: dict[str, str]) -> None:
    r.section("Disco y vídeos")
    library = Path(setting(env, "MEDIA_LIBRARY_DIR", str(BASE / "media" / "library")))
    where = library if library.exists() else BASE
    usage = shutil.disk_usage(where)
    low = usage.free < max(10 * 1024 ** 3, usage.total * 0.05)
    r.line(WARN if low else OK, f"{gb(usage.free)} libres de {gb(usage.total)}",
           "libera espacio: la biblioteca y la base de datos lo necesitan" if low else "")
    if library.exists():
        folders = [p for p in library.iterdir() if p.is_dir() and not p.name.startswith(".")]
        r.line(OK, f"Biblioteca {library} · {len(folders)} carpeta(s)")
    else:
        r.line(OK, f"La biblioteca ({library}) se crea al arrancar")
    legacy = [d for d in ("inbox", "posted", "failed") if any((BASE / "media" / d).glob("*.mp4"))]
    if legacy:
        r.line(WARN, "Quedan vídeos de antes de la biblioteca en media/" + ", media/".join(legacy),
               "tools/library_finalize.py (con la app cerrada) los pasa y libera el espacio")


def check_ai(r: Report, env: dict[str, str], offline: bool) -> None:
    r.section("Textos automáticos")
    if setting(env, "CAPTION_AI_ENABLED", "true").lower() in ("false", "0", "no"):
        r.line(OK, "Desactivados (CAPTION_AI_ENABLED=false)")
        return
    model = setting(env, "WHISPER_MODEL", "small")
    models_dir = BASE / "data" / "models" / "whisper"
    cached = models_dir.exists() and any(model in p.name.lower() for p in models_dir.iterdir())
    r.line(OK if cached else WARN, f"Whisper «{model}» " + ("descargado" if cached else "sin descargar (~460 MB la primera vez)"),
           "" if cached else "make models")
    url = setting(env, "OLLAMA_URL", "http://127.0.0.1:11434")
    if not url:
        r.line(OK, "Sin Ollama (OLLAMA_URL vacío): el texto será la primera frase del vídeo")
        return
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=3) as resp:
            names = [m.get("name", "") for m in json.load(resp).get("models", [])]
    except Exception as exc:  # noqa: BLE001
        r.line(WARN, f"Ollama no responde en {url} ({type(exc).__name__})",
               "brew install ollama && brew services start ollama   (u ollama serve)")
        return
    wanted = setting(env, "OLLAMA_MODEL", "")
    if wanted and wanted not in names:
        r.line(FAIL, f"OLLAMA_MODEL={wanted} no está instalado (hay: {', '.join(names) or 'ninguno'})",
               f"ollama pull {wanted}")
    elif not names:
        r.line(WARN, "Ollama responde pero no tiene ningún modelo", f"make ollama   (ollama pull {RECOMMENDED_MODEL})")
    else:
        r.line(OK, f"Ollama en {url} · modelo {wanted or '(el más pequeño de 4 GB o más)'} · "
               f"{len(names)} instalado(s)")


def check_network(r: Report, env: dict[str, str], offline: bool) -> None:
    r.section("Red")
    if offline:
        r.line(OK, "Sin comprobar (--offline)")
        return
    base = setting(env, "ZERNIO_API_BASE", "https://zernio.com/api/v1").rstrip("/")
    status = http_status(f"{base}/accounts")
    r.line(OK if status in (200, 401, 403) else FAIL, f"API de Zernio ({base}): {status}"
           + (" (responde; sin clave es lo esperado)" if status in (401, 403) else ""),
           "" if status in (200, 401, 403) else "sin conexión con Zernio no se publica nada")
    # The upload leg: the bytes go to Cloudflare R2, not to zernio.com, and an
    # ISP has blackholed that prefix before (docs/TROUBLESHOOTING.md): the API
    # answers and every upload hangs. What matters is whether it connects.
    host = "r2.cloudflarestorage.com"
    try:
        addresses = sorted({info[4][0] for info in socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)})
    except OSError as exc:
        r.line(WARN, f"Ruta de subida ({host}): no se resuelve ({exc})")
        return
    reached = []
    for address in addresses[:4]:
        try:
            with socket.create_connection((address, 443), timeout=6):
                reached.append(address)
        except OSError:
            pass
    r.line(OK if reached else WARN,
           f"Ruta de subida ({host}): conecta con {', '.join(reached)}" if reached
           else f"Ruta de subida ({host}): ninguna de {', '.join(addresses)} contesta",
           "" if reached else "las subidas se colgarán: la app prueba otra ruta sola; si persiste, es la red "
                              "(el proveedor bloquea ese rango de Cloudflare a ratos)")


def check_port(r: Report, env: dict[str, str]) -> None:
    r.section("Puerto")
    port = int(setting(env, "PORT", "8787"))
    with socket.socket() as sock:
        sock.settimeout(0.5)
        busy = sock.connect_ex(("127.0.0.1", port)) == 0
    if not busy:
        r.line(OK, f"El puerto {port} está libre (la app no está abierta)")
        return
    status = http_status(f"http://127.0.0.1:{port}/api/health", timeout=3)
    r.line(OK if status == 200 else WARN,
           f"En http://127.0.0.1:{port} " + ("responde la app" if status == 200 else f"hay otra cosa ({status})"),
           "" if status == 200 else "cambia PORT en .env")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="no comprobar Zernio ni la ruta de subida")
    args = parser.parse_args()
    env = read_env(BASE / ".env")
    r = Report()
    print(f"Scheduler · diagnóstico de {platform.node()} · macOS {platform.mac_ver()[0] or platform.platform()}")
    check_python(r)
    check_tools(r)
    check_config(r, env)
    accounts = check_database(r, env)
    check_secrets(r, env, accounts)
    check_disk(r, env)
    check_ai(r, env, args.offline)
    check_network(r, env, args.offline)
    check_port(r, env)
    print()
    if r.failed:
        print(f"{FAIL} Hay cosas que impiden que funcione: arriba está cómo arreglarlas.")
        return 1
    print(f"{WARN} Funciona, con avisos." if r.warned else f"{OK} Todo en orden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
