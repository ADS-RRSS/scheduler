"""Optional outgoing notifications (Discord, Slack, ntfy, Telegram, anything).

Fire-and-forget on purpose: a broken webhook must never break a publish.

The same URL field accepts several kinds of endpoint; the request is shaped to
match, so no extra configuration is needed:

    https://discord.com/api/webhooks/...          Discord (content)
    https://hooks.slack.com/services/...          Slack / Mattermost (text)
    https://ntfy.sh/<topic>                       ntfy push (plain text + Title header)
    https://api.telegram.org/bot<token>/sendMessage?chat_id=<id>   Telegram bot
    anything else                                 JSON with content/text/title/message
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import httpx

log = logging.getLogger("scheduler")


@dataclass
class Outgoing:
    url: str
    kind: str
    json: dict | None = None
    content: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


def _kind_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("discord.com") or host.endswith("discordapp.com"):
        return "discord"
    if host.endswith("slack.com"):
        return "slack"
    if host == "ntfy.sh" or host.startswith("ntfy.") or ".ntfy." in host:
        return "ntfy"
    if host == "api.telegram.org":
        return "telegram"
    return "generic"


def _plain(title: str, message: str, extra: dict | None) -> str:
    text = f"{title}\n{message}"
    if extra:
        text += "\n" + "\n".join(f"- {k}: {v}" for k, v in extra.items())
    return text


def prepare(webhook_url: str, title: str, message: str, extra: dict | None = None) -> Outgoing:
    """Build the request for ``webhook_url`` without sending it (unit-testable)."""
    kind = _kind_of(webhook_url)
    markdown = f"**{title}**\n{message}"
    if extra:
        markdown += "\n" + "\n".join(f"- {k}: {v}" for k, v in extra.items())

    if kind == "discord":
        return Outgoing(webhook_url, kind, json={"content": markdown[:2000]})
    if kind == "slack":
        return Outgoing(webhook_url, kind, json={"text": markdown})
    if kind == "ntfy":
        # ntfy takes the body as the message; headers carry the title/priority.
        # Header values must be Latin-1 safe, so anything else is stripped.
        safe_title = title.encode("latin-1", "ignore").decode("latin-1").strip() or "Scheduler"
        return Outgoing(
            webhook_url, kind, content=_plain(title, message, extra),
            headers={"Title": safe_title, "Priority": "high", "Tags": "movie_camera",
                     "Content-Type": "text/plain; charset=utf-8"},
        )
    if kind == "telegram":
        parsed = urlparse(webhook_url)
        chat_id = (parse_qs(parsed.query).get("chat_id") or [""])[0]
        url = parsed._replace(query="").geturl()
        return Outgoing(url, kind, json={"chat_id": chat_id,
                                         "text": _plain(title, message, extra)[:4096],
                                         "disable_web_page_preview": True})
    # `content` suits Discord-compatible receivers, `text` Slack-compatible ones:
    # sending both lets an unknown URL work with either.
    return Outgoing(webhook_url, kind, json={"content": markdown, "text": markdown,
                                             "title": title, "message": message,
                                             "extra": extra or {}})


async def send(webhook_url: str, title: str, message: str, extra: dict | None = None) -> bool:
    if not webhook_url:
        return False
    try:
        out = prepare(webhook_url, title, message, extra)
        async with httpx.AsyncClient(timeout=10) as client:
            if out.content is not None:
                resp = await client.post(out.url, content=out.content.encode("utf-8"),
                                         headers=out.headers)
            else:
                resp = await client.post(out.url, json=out.json, headers=out.headers)
        if resp.status_code >= 400:
            log.warning("webhook %s answered %s: %s", out.kind, resp.status_code, resp.text[:120])
        return resp.status_code < 400
    except Exception as exc:  # noqa: BLE001 - notifications are best-effort
        log.warning("webhook failed: %s", exc)
        return False


def resolve_url(session, settings) -> str:
    """The webhook URL the user actually configured: UI setting first, then .env."""
    from ..models import Setting

    row = session.get(Setting, "webhook_url")
    from_ui = ((row.value or {}).get("value") if row else "") or ""
    return from_ui.strip() or (settings.webhook_url or "").strip()
