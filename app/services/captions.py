"""Caption rendering: spintax variants + placeholders + hashtag rotation.

Posting the exact same caption dozens of times is the single most obvious
"this is a bot" signal, so every caption is rendered from a template.
"""
from __future__ import annotations

import random
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from ..models import Setting

MAX_CAPTION_CHARS = 2200  # TikTok's documented limit, in UTF-16 runes

_PLACEHOLDER_RE = re.compile(r"\[([a-z_]+)\]")


def expand_spintax(text: str, rng: random.Random | None = None) -> str:
    """Resolve `{a|b|c}` groups, innermost first, supporting nesting.

    `{hola|buenas}` -> "hola" or "buenas".
    Braces without a `|` are left untouched so they can't clash with anything.
    """
    rng = rng or random.Random()
    pattern = re.compile(r"\{([^{}]*)\}")
    # Bound the loop: deeply nested or malformed input must not spin forever.
    for _ in range(50):
        match = pattern.search(text)
        if not match:
            break
        body = match.group(1)
        replacement = rng.choice(body.split("|")) if "|" in body else match.group(0)
        if replacement == match.group(0):
            # No alternatives: skip past it to avoid rematching the same group.
            rest = expand_spintax(text[match.end():], rng)
            return text[: match.end()] + rest
        text = text[: match.start()] + replacement + text[match.end():]
    return text


def render_caption(
    template: str,
    *,
    hashtags: list[str] | None = None,
    hashtag_count: int = 0,
    filename: str = "",
    index: int = 0,
    now: datetime | None = None,
    timezone_name: str = "Europe/Madrid",
    rng: random.Random | None = None,
    keep_order: bool = False,
    limit: int | None = MAX_CAPTION_CHARS,
) -> str:
    """Turn a caption template into the final string sent to TikTok.

    Placeholders (square brackets, so they never collide with spintax braces):
      [fecha] [hora] [dia] [mes] [indice] [archivo] [hashtags]

    Hashtags are shuffled by default, one more thing that keeps two captions
    from ever being identical. ``keep_order`` is for lists that were built in
    a deliberate order (fixed tags first, reach tags last). ``limit=None``
    skips the final cut, for the editor to tell how far over it goes.
    """
    rng = rng or random.Random()
    now = (now or datetime.now(ZoneInfo(timezone_name))).astimezone(ZoneInfo(timezone_name))

    text = expand_spintax(template or "", rng)

    chosen: list[str] = []
    if hashtags:
        pool = [h if h.startswith("#") else f"#{h}" for h in hashtags if h.strip()]
        k = min(hashtag_count or len(pool), len(pool))
        if keep_order and k >= len(pool):
            chosen = list(pool)
        else:
            chosen = rng.sample(pool, k) if k else []

    days = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    months = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
              "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    values = {
        "fecha": now.strftime("%d/%m/%Y"),
        "hora": now.strftime("%H:%M"),
        "dia": days[now.weekday()],
        "mes": months[now.month - 1],
        "indice": str(index),
        "archivo": Path(filename).stem if filename else "",
        "hashtags": " ".join(chosen),
    }
    text = _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)

    # Append hashtags when the template never referenced them explicitly: on
    # the same line after a one-liner, on their own line under a description
    # written in lines (hook, fact, question).
    if chosen and "[hashtags]" not in (template or ""):
        separator = "\n" if "\n" in text.strip() else " "
        text = f"{text.rstrip()}{separator}{' '.join(chosen)}".strip()

    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return _truncate(text, limit) if limit else text


def _truncate(text: str, limit: int) -> str:
    """Trim to the limit without cutting a hashtag or a word in half."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = cut.rfind(" ")
    return (cut[:boundary] if boundary > limit * 0.6 else cut).rstrip()


def stored_hashtags(session: Session) -> list[str]:
    """The fixed hashtags saved in Ajustes, as ``#tag`` strings."""
    row = session.get(Setting, "hashtags")
    return parse_hashtags((row.value or {}).get("value", "")) if row else []


def parse_hashtags(raw: str) -> list[str]:
    """Accept hashtags separated by spaces, commas or newlines."""
    if not raw:
        return []
    parts = re.split(r"[\s,]+", raw.strip())
    out: list[str] = []
    for part in parts:
        tag = part.strip().lstrip("#")
        if tag and f"#{tag}" not in out:
            out.append(f"#{tag}")
    return out
