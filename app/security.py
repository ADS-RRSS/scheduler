"""Optional passcode gate for the local UI, and the guard in front of it.

The passcode is a lock on a door that is already inside your house: the server
binds to 127.0.0.1, so it only stops other processes / people on this machine.
It is not a substitute for not exposing the port publicly.

The guard covers what a lock on the door cannot: a web page open in the same
browser. Any site can make the browser send a POST to 127.0.0.1:8787 (a form,
or a fetch without body that needs no permission), and a site whose name
resolves to 127.0.0.1 a moment later (DNS rebinding) can even read the
answers. So a request that changes something must come from this app's own
pages (``Sec-Fetch-Site``/``Origin``), and the ``Host`` it was addressed to
must be one of this app's names. curl and the tools send neither header and
are let through: they are not a browser carrying someone else's page.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from .config import get_settings
from .crypto import _load_or_create_key

COOKIE_NAME = "tts_session"
PUBLIC_PATHS = ("/login", "/static/", "/favicon.ico", "/api/health")


def _expected_token() -> str:
    settings = get_settings()
    return hmac.new(
        _load_or_create_key(), settings.ui_passcode.encode(), hashlib.sha256
    ).hexdigest()


def verify_passcode(candidate: str) -> bool:
    settings = get_settings()
    return bool(settings.ui_passcode) and secrets.compare_digest(
        candidate.encode(), settings.ui_passcode.encode()
    )


def issue_cookie(response) -> None:
    response.set_cookie(
        COOKIE_NAME, _expected_token(),
        httponly=True, samesite="lax", max_age=30 * 24 * 3600, path="/",
    )


def is_authenticated(request: Request) -> bool:
    settings = get_settings()
    if not settings.ui_passcode:
        return True
    cookie = request.cookies.get(COOKIE_NAME, "")
    return secrets.compare_digest(cookie.encode(), _expected_token().encode())


async def passcode_middleware(request: Request, call_next):
    settings = get_settings()
    path = request.url.path
    if not settings.ui_passcode or path.startswith(PUBLIC_PATHS):
        return await call_next(request)
    if is_authenticated(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "No autenticado"}, status_code=401)
    return RedirectResponse("/login", status_code=302)


# --------------------------------------------------------------------------- #
# The guard
# --------------------------------------------------------------------------- #

LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def allowed_hosts(settings) -> frozenset[str] | None:
    """The names this app answers to; None when any name will do.

    ``ALLOWED_HOSTS`` adds names (``*`` = any). The loopback names are always
    allowed: a rebinding page makes the browser send its own name, never
    ``127.0.0.1``, and the container's health check and an SSH tunnel arrive
    as ``127.0.0.1``. Bound to every interface (Docker, a LAN) with no list,
    there is nothing to compare with, and the startup banner says so.
    """
    raw = (settings.allowed_hosts or "").strip()
    if raw == "*":
        return None
    if raw:
        return LOOPBACK_NAMES | {h.strip().lower() for h in raw.split(",") if h.strip()}
    if settings.host in LOOPBACK_NAMES:
        return LOOPBACK_NAMES
    return None


def _hostname(netloc: str) -> str:
    """``127.0.0.1:8787`` -> ``127.0.0.1``; ``[::1]:8787`` -> ``[::1]``."""
    netloc = (netloc or "").strip().lower()
    if netloc.startswith("["):
        return netloc[: netloc.find("]") + 1] if "]" in netloc else netloc
    return netloc.rsplit(":", 1)[0] if netloc.count(":") == 1 else netloc


def _blocked(detail: str, status: int) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


async def request_guard(request: Request, call_next):
    settings = get_settings()
    host = request.headers.get("host", "")
    hosts = allowed_hosts(settings)
    if hosts is not None and _hostname(host) not in hosts:
        return _blocked(f"Host no permitido: {_hostname(host) or '(vacío)'}. Añádelo a "
                        "ALLOWED_HOSTS en .env si es el nombre con el que abres la app.", 400)
    if request.method in UNSAFE_METHODS:
        site = request.headers.get("sec-fetch-site", "")
        origin = request.headers.get("origin")
        # A proxy may rewrite Host; the name the browser used survives here.
        names = {host.lower(), request.headers.get("x-forwarded-host", "").lower()} - {""}
        if site in ("cross-site", "same-site") or (
                origin is not None and urlsplit(origin).netloc.lower() not in names):
            return _blocked("Petición bloqueada: viene de otra página web, no de esta "
                            "aplicación.", 403)
    return await call_next(request)
