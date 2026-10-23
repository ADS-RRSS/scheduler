"""Test environment.

Every environment variable must be set BEFORE anything under `app` is imported:
`app.config.get_settings()` is cached and `app.db` builds the engine at import
time, so a late override would be silently ignored.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

MOCK_PORT = 8899
MOCK_BASE = f"http://127.0.0.1:{MOCK_PORT}"
APP_PORT = 8788

_TMP = Path(tempfile.mkdtemp(prefix="tts-test-"))
for sub in ("library", "inbox", "posted", "failed", "secrets", "data", "backups"):
    (_TMP / sub).mkdir(parents=True, exist_ok=True)

os.environ.update({
    "DATABASE_URL": f"sqlite:///{_TMP / 'data' / 'test.db'}",
    "MEDIA_LIBRARY_DIR": str(_TMP / "library"),
    "MEDIA_INBOX_DIR": str(_TMP / "inbox"),
    "MEDIA_POSTED_DIR": str(_TMP / "posted"),
    "MEDIA_FAILED_DIR": str(_TMP / "failed"),
    "SECRETS_DIR": str(_TMP / "secrets"),
    "BACKUPS_DIR": str(_TMP / "backups"),
    "PORT": str(APP_PORT),
    "STATUS_POLL_SECONDS": "0",
    "SCHEDULER_TICK_SECONDS": "3600",
    "UI_PASSCODE": "",
    # httpx's test client calls the app "testserver".
    "ALLOWED_HOSTS": "testserver,127.0.0.1,localhost",
    # The worker's read-only extras stay off unless a test asks for them, so a
    # plain tick() never reaches out for analytics or comments.
    "ANALYTICS_REFRESH_MINUTES": "0",
    "COMMENTS_ENABLED": "false",
    # Automatic texts stay off too: with them on, every uploaded video would
    # wait for Whisper before publishing. test_autocaption.py turns them on.
    "CAPTION_AI_ENABLED": "false",
    "OLLAMA_URL": "",
    "WEBHOOK_URL": "",
    # The daily copy of the database too: test_backups.py turns it on.
    "BACKUP_AUTO_HOURS": "0",
    # The fake Zernio lives in tools/mock_zernio.py.
    "ZERNIO_API_BASE": f"{MOCK_BASE}/v1",
    "ZERNIO_TIMEOUT_SECONDS": "60",
    "ZERNIO_URL_GRACE_MINUTES": "5",
})


@pytest.fixture(scope="session")
def tmp_root() -> Path:
    yield _TMP
    shutil.rmtree(_TMP, ignore_errors=True)


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="session")
def mock_server():
    """Run the fake Zernio API for the whole session.

    If something is already listening on the port (typically tools/demo.sh left
    running) reuse it instead of starting a second server that would fail to
    bind and surface as a confusing unhandled-thread exception.
    """
    if _port_open(MOCK_PORT):
        yield MOCK_BASE
        return

    import uvicorn

    from tools.mock_zernio import app as mock_app

    config = uvicorn.Config(mock_app, host="127.0.0.1", port=MOCK_PORT, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(100):
        if _port_open(MOCK_PORT):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("el servidor mock no arrancó")

    yield MOCK_BASE

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def sample_video(tmp_root: Path) -> Path:
    """A real 1080x1920 H.264 + AAC clip, so ffprobe has something to chew on."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg no disponible")
    out = tmp_root / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30:duration=4",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
            "-c:a", "aac", "-shortest", str(out),
        ],
        check=True, capture_output=True,
    )
    return out


@pytest.fixture
async def client(mock_server, fresh_db):
    """An HTTP client bound to the app, with a clean database.

    The ASGI transport skips the lifespan on purpose: the background worker must
    not tick on its own while a test is driving it step by step.
    """
    import httpx

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture
def fresh_db():
    """Drop and recreate every table, and empty the working dirs, between tests.

    Clearing the library matters: it is the watch folder, scanned on every
    worker tick, so files left behind by a previous test would be registered
    again in the next one.
    """
    from sqlalchemy import inspect, text

    from app.db import engine, init_db
    from app.models import Base

    Base.metadata.drop_all(engine)
    # Tables a migration test may have created outside the models (e.g. the
    # legacy oauth_states) must not leak into the next test either.
    with engine.begin() as conn:
        for name in inspect(conn).get_table_names():
            conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
    init_db()
    for sub in ("inbox", "posted", "failed", "backups"):
        for path in (_TMP / sub).iterdir():
            if path.is_file():
                path.unlink()
    library = _TMP / "library"
    shutil.rmtree(library, ignore_errors=True)
    library.mkdir(parents=True, exist_ok=True)
    from app.services import library as library_svc
    library_svc._ignored.clear()
    yield
