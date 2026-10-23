"""Notifications are best-effort: they must never break a publish."""
from __future__ import annotations

from app.services import notify


async def test_no_url_is_a_silent_no_op():
    assert await notify.send("", "titulo", "mensaje") is False


async def test_unreachable_webhook_returns_false_instead_of_raising():
    # Port 1 on localhost refuses instantly: no waiting on a timeout.
    assert await notify.send("http://127.0.0.1:1/hook", "titulo", "mensaje") is False


async def test_malformed_url_returns_false_instead_of_raising():
    assert await notify.send("no-es-una-url", "titulo", "mensaje") is False


async def test_extra_fields_do_not_break_the_payload():
    assert await notify.send(
        "http://127.0.0.1:1/hook", "Vídeo publicado", "clip.mp4",
        {"sonido a añadir (volumen 0)": "Tema — https://tiktok.com/music/x-1"},
    ) is False
