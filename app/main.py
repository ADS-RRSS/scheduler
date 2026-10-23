"""FastAPI application entry point."""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError

from .api.routes_admin import router as admin_router
from .api.routes_api import router as api_router
from .api.routes_calendar import router as calendar_router
from .api.routes_db import router as db_router
from .api.routes_library import router as library_router
from .api.routes_text import router as text_router
from .config import APP_VERSION, get_settings
from .db import init_db, session_scope
from .scheduler.captioner import captioner
from .scheduler.worker import worker
from .services import instance, library, scheduling
from .security import (
    LOOPBACK_NAMES,
    issue_cookie,
    passcode_middleware,
    request_guard,
    verify_passcode,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scheduler")

WEB_DIR = Path(__file__).parent / "web"


# Set by run(): the port in the settings is the one uvicorn is about to bind.
PORT_CHECK_ENV = "TTS_CHECK_PORT"


def ensure_port_free(settings) -> None:
    """Refuse to start when the port is taken, before the worker exists.

    uvicorn binds the port after the startup code has run, so a second copy
    would start publishing for a moment before failing on the port. A copy
    from before the lease existed takes none: the port is all it leaves.
    """
    if os.environ.get(PORT_CHECK_ENV) != "1":
        return
    family = socket.AF_INET6 if ":" in settings.host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((settings.host, settings.port))
        except OSError as exc:
            message = (f"El puerto {settings.port} ya está en uso ({exc.strerror}): ¿la app ya está "
                       "abierta en otra terminal? Ciérrala o cambia PORT en .env.")
            log.error(message)
            raise RuntimeError(message) from exc


def take_database(settings, *, fresh: bool = False) -> None:
    """Hold the lease on the database, or refuse to start (services/instance.py).

    Called before anything is touched — the uploads being received, the
    migrations — and again once the tables exist, for a brand-new database.
    """
    try:
        with session_scope() as session:
            replaced = instance.acquire(session, settings.port,
                                        takeover=settings.instance_takeover,
                                        marker=instance.marker_path(settings.database_path))
    except OperationalError:
        if fresh:
            raise
        return  # no tables yet: nobody can be using it
    except instance.InstanceConflict as exc:
        log.error("=" * 68)
        log.error("  %s", exc)
        log.error("=" * 68)
        raise
    if replaced and not fresh:
        log.warning("base de datos tomada de %s", instance.describe(replaced))


def _renew_lease(port: int, marker) -> bool:
    with session_scope() as session:
        return instance.heartbeat(session, port, marker=marker)


async def keep_lease(port: int, marker) -> None:
    """Renew the lease; stop publishing if another copy took the database."""
    while True:
        await asyncio.sleep(instance.HEARTBEAT_SECONDS)
        try:
            # In a thread: on a busy database SQLite waits up to its timeout,
            # and that wait must not freeze the event loop (and every request).
            ours = await asyncio.to_thread(_renew_lease, port, marker)
        except Exception as exc:  # noqa: BLE001 - a busy database must not kill the app
            log.warning("no se pudo renovar el uso de la base de datos: %s", exc)
            continue
        if not ours:
            log.error("Otra copia de la aplicación ha tomado la base de datos: este "
                      "planificador deja de publicar. Cierra una de las dos.")
            await captioner.stop()
            await worker.stop()
            return


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    ensure_port_free(settings)
    take_database(settings)
    library.clear_incoming(settings.media_library_dir)
    if init_db():
        # The schema moved, so the rules may have too: anything still pinned
        # was planned by the previous version and is recomputed here once.
        with session_scope() as session:
            touched = scheduling.replan_all(session)
        log.info("esquema actualizado: %s cola(s) replanificadas", touched)
    take_database(settings, fresh=True)
    await worker.start()
    if settings.caption_ai_enabled:
        await captioner.start()
    marker = instance.marker_path(settings.database_path)
    lease = asyncio.create_task(keep_lease(settings.port, marker), name="instance-lease")
    banner(settings)
    try:
        yield
    finally:
        lease.cancel()
        await captioner.stop()
        await worker.stop()
        try:
            with session_scope() as session:
                instance.release(session, marker)
        except Exception as exc:  # noqa: BLE001 - shutting down anyway
            log.warning("no se pudo liberar la base de datos: %s", exc)


def banner(settings) -> None:
    log.info("=" * 68)
    log.info("  Scheduler %s  ->  %s", APP_VERSION, settings.base_url)
    log.info("  Publica a través de Zernio (%s)", settings.zernio_api_base)
    log.info("  Biblioteca: %s", settings.media_library_dir)
    if settings.caption_ai_enabled:
        log.info("  Textos automáticos: Whisper «%s» · Ollama %s", settings.whisper_model,
                 settings.ollama_url or "desactivado")
    else:
        log.info("  Textos automáticos desactivados (CAPTION_AI_ENABLED=false)")
    if not settings.ui_passcode:
        log.info("  Sin código de acceso: las páginas de administración quedan abiertas a")
        log.info("  cualquier proceso local. Pon UI_PASSCODE en .env si compartes el equipo.")
    if settings.host not in LOOPBACK_NAMES:
        log.warning("  Escuchando en %s: cualquiera que alcance este puerto ve la app.", settings.host)
        if not settings.ui_passcode:
            log.warning("  Pon UI_PASSCODE en .env (en Docker, el puerto sólo se publica en 127.0.0.1).")
        if not settings.allowed_hosts:
            log.warning("  Pon ALLOWED_HOSTS con el nombre con el que la abres (protege del DNS rebinding).")
    log.info("=" * 68)


class RevalidatingStaticFiles(StaticFiles):
    """Serve assets with `no-cache`.

    Without an explicit header the browser may keep using a cached app.js for
    hours. On a tool you edit and restart locally that means silently running
    yesterday's code. `no-cache` still allows 304s, so nothing is re-downloaded
    unless it actually changed.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app = FastAPI(
    title="Scheduler",
    description="Programador local de subidas a TikTok a través de Zernio",
    version=APP_VERSION,
    lifespan=lifespan,
)
app.middleware("http")(passcode_middleware)
# Added last, so it runs first: a request from another web page never reaches
# the passcode check, let alone a route.
app.middleware("http")(request_guard)
app.include_router(api_router)
app.include_router(calendar_router)
app.include_router(admin_router)
app.include_router(db_router)
app.include_router(library_router)
app.include_router(text_router)
app.mount("/static", RevalidatingStaticFiles(directory=WEB_DIR / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    # Same reasoning as the static files: after an update the browser must not
    # keep showing yesterday's page.
    return HTMLResponse(
        (WEB_DIR / "templates" / "index.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/login", response_class=HTMLResponse)
def login_form(error: str = "") -> HTMLResponse:
    note = (
        '<p style="color:#f87171">Código incorrecto.</p>' if error else ""
    )
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8"><title>Scheduler</title>
<style>
 body{{font:16px system-ui,-apple-system,sans-serif;display:grid;place-items:center;
      height:100vh;margin:0;background:#0b0b0f;color:#e7e7ea}}
 form{{background:#16161c;border:1px solid #26262f;padding:2rem;border-radius:16px;
      display:grid;gap:1rem;width:min(22rem,90vw)}}
 input,button{{padding:.7rem .9rem;border-radius:10px;border:1px solid #2e2e3a;
      background:#0f0f14;color:inherit;font:inherit}}
 button{{background:#7c5cff;border-color:#7c5cff;cursor:pointer;font-weight:600}}
</style>
<form method="post" action="/login">
  <strong>Scheduler</strong>
  {note}
  <input type="password" name="passcode" placeholder="Código de acceso" autofocus>
  <button type="submit">Entrar</button>
</form>""",
        status_code=200,
    )


@app.post("/login")
def login_submit(passcode: str = Form("")):
    if not verify_passcode(passcode):
        return RedirectResponse("/login?error=1", status_code=302)
    response = RedirectResponse("/", status_code=302)
    issue_cookie(response)
    return response


@app.get("/favicon.ico")
def favicon():
    # Inline 1x1 transparent GIF: avoids a 404 in the console without a binary asset.
    return Response(
        content=(
            b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
            b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
            b"\x00\x02\x02D\x01\x00;"
        ),
        media_type="image/gif",
    )


def run() -> None:
    """Console entry point: `python -m app.main`."""
    import uvicorn

    settings = get_settings()
    os.environ[PORT_CHECK_ENV] = "1"
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    run()
