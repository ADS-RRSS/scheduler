"""Outgoing notifications: one URL field, several kinds of receiver.

Also pins down that the webhook configured from the UI is the one used:
`notify.resolve_url` must prefer the stored setting over the .env value.
"""
from __future__ import annotations

import pytest

from app.services import notify
from tests.helpers import (
    NOTIFY_URL,
    drain,
    make_account,
    mock_state,
    put_schedule,
    reset_mock,
    upload_sample,
)


def test_discord_payload_uses_content():
    out = notify.prepare("https://discord.com/api/webhooks/1/abc", "Título", "msg", {"k": "v"})
    assert out.kind == "discord"
    assert out.json == {"content": "**Título**\nmsg\n- k: v"}


def test_slack_payload_uses_text():
    out = notify.prepare("https://hooks.slack.com/services/T/B/x", "Título", "msg")
    assert out.kind == "slack" and out.json == {"text": "**Título**\nmsg"}


def test_ntfy_sends_plain_text_with_title_header():
    out = notify.prepare("https://ntfy.sh/mi-tema", "Vídeo publicado ✅", "clip.mp4", {"a": 1})
    assert out.kind == "ntfy"
    assert out.json is None
    assert out.content == "Vídeo publicado ✅\nclip.mp4\n- a: 1"
    assert out.headers["Title"] == "Vídeo publicado"  # header values must be latin-1
    assert out.headers["Priority"] == "high"


def test_telegram_moves_chat_id_into_the_body():
    out = notify.prepare(
        "https://api.telegram.org/bot123:ABC/sendMessage?chat_id=42", "T", "m"
    )
    assert out.kind == "telegram"
    assert out.url == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert out.json["chat_id"] == "42" and out.json["text"] == "T\nm"


def test_generic_payload_carries_both_conventions():
    out = notify.prepare("https://example.invalid/hook", "T", "m", {"x": "y"})
    assert out.kind == "generic"
    assert out.json["content"] == out.json["text"] == "**T**\nm\n- x: y"
    assert out.json["title"] == "T" and out.json["extra"] == {"x": "y"}


@pytest.mark.usefixtures("fresh_db")
def test_ui_setting_wins_over_env():
    from app.config import get_settings
    from app.db import session_scope
    from app.models import Setting

    settings = get_settings()
    with session_scope() as session:
        assert notify.resolve_url(session, settings) == (settings.webhook_url or "")
        session.add(Setting(key="webhook_url", value={"value": " https://ntfy.sh/x "}))
    with session_scope() as session:
        assert notify.resolve_url(session, settings) == "https://ntfy.sh/x"


async def test_publication_is_announced_to_the_configured_webhook(client, sample_video):
    await reset_mock()
    await client.put("/api/settings", json={"hashtags": "", "webhook_url": NOTIFY_URL})
    account_id = await make_account(client, label="Principal")
    await put_schedule(client, account_id)
    await upload_sample(client, account_id, sample_video, caption="Aviso [hashtags]")
    await drain(account_id)

    calls = (await mock_state())["notify_calls"]
    assert len(calls) == 1
    content = calls[0]["payload"]["content"]
    assert "Vídeo publicado" in content and "Principal" in content
    assert "tiktok.com/@relay_user/video/" in content


async def test_webhook_test_endpoint_reports_the_kind(client):
    await reset_mock()
    await client.put("/api/settings", json={"hashtags": "", "webhook_url": NOTIFY_URL})
    resp = await client.post("/api/settings/webhook-test")
    assert resp.status_code == 200 and resp.json()["kind"] == "generic"
    assert len((await mock_state())["notify_calls"]) == 1

    await client.put("/api/settings", json={"hashtags": "", "webhook_url": ""})
    assert (await client.post("/api/settings/webhook-test")).status_code == 400
