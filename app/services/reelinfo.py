"""The information sheet that can travel next to a video.

A reel generator (ContentBuilder) writes, next to every ``reel_07_….mp4``, a
``reel_07_….txt`` with everything it knows about that reel.

**Format 2** (since October 2026) carries the final texts of each platform,
written by ContentBuilder for search and reach on TikTok and YouTube Shorts::

    ContentBuilder · ficha de publicación · v2

    · vídeo: reel_07_….mp4
    · duración: 46,8 s
    · portada: <the title card>
    · serie: <the series> · 7/50
    · palabra clave: <what people search for>
    · textos: plan externo | modelo local | plantilla
    · verificación: publicable | ⚠ revisar a mano — <what to check>

    — tiktok —
    · título: …
    · descripción:
    <one line per sentence; the first is what shows before «más»>
    · hashtags: #… #…
    · comentario fijado: …

    — youtube —
    (the same; long videos add «· etiquetas: …, …»)

    — guion —
    <the full script, as it is spoken>

    — control —
    · gancho: …
    · idea: …

Those texts can be published as they are, with no Whisper and no Ollama
(:func:`texts_for`); the library does so unless the user asks for the texts to
be rewritten with AI. The duration lets the library check, for free, that the
sheet belongs to the video (:func:`duration_match`).

**Format 1** (before October 2026)::

    <the full script, exactly as it is spoken>

    —
    <the opening line>
    · portada: <the title card>

    — pie de publicación —
    <a draft caption line, with keywords>
    <the series description · 7/50>
    #hashtag #hashtag …

    — comentario fijado —
    · <a question for the pinned comment>
    · <another one>

    · idea: <the idea of the reel>
    · verificación: publicable | ⚠ revisar a mano — <what to check>

That is not a caption: published as is, it would put the whole script on
TikTok. It is *context*, far better than a transcript — the exact words, the
title, the niche hashtags, the questions — so the captioner writes the real
text from it (:mod:`app.services.autocaption`), or, without AI, takes its
title, first question and hashtags. A ``.txt`` without these markers is still
what it always was: a caption written by hand, final.

Before trusting a sheet, :func:`script_match` compares its script with what
Whisper hears in the first seconds of the video: the file name says they
belong together, the audio confirms it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .hashtags import fold

# Section headers, Spanish and English (ContentBuilder writes both).
_HEADER_RE = re.compile(r"^—\s*(.+?)\s*—$")
# Format 2: the first line says what the file is, and its version. Sheets
# written before ContentBuilder was renamed still sign as PhrasesReels.
_SIGNATURE_RE = re.compile(r"^\s*(?:ContentBuilder|PhrasesReels)\b.*?\bv\s*(\d+)\s*$", re.IGNORECASE)
PLATFORMS = ("tiktok", "youtube")
# In format 2 the script comes last but one, and only these end it: a long
# script may hold dialogue between dashes that looks like a header.
_AFTER_SCRIPT = {"control", *PLATFORMS}
_SCRIPT_HEADERS = {"guion", "script"}
# The fields of a platform section, and what they are called here.
_PLATFORM_KEYS = {
    "titulo": "title", "title": "title",
    "descripcion": "description", "description": "description",
    "hashtags": "tags",
    "etiquetas": "keywords", "tags": "keywords", "keywords": "keywords",
    "comentario fijado": "first_comment", "comentario": "first_comment",
    "pinned comment": "first_comment", "first comment": "first_comment",
}
# How far the sheet's duration may be from the video's: a re-encode moves it
# by a frame or two, a different reel of the series by seconds.
DURATION_TOLERANCE = 1.0
DURATION_TOLERANCE_SHARE = 0.03
_CAPTION_HEADERS = {"pie de publicacion", "caption"}
_PIN_HEADERS = {"comentario fijado", "pinned comment"}
# "· clave: valor" lines.
_META_RE = re.compile(r"^·\s*([^:]{2,30}):\s*(.*)$")
_TITLE_KEYS = {"portada", "title card"}
_IDEA_KEYS = {"idea"}
_VERDICT_KEYS = {"verificacion", "fact-check", "fact check"}
_TAG_RE = re.compile(r"#[^\s#]+")
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
# A series counter such as "4/50": internal bookkeeping, never for viewers.
SERIES_COUNTER_RE = re.compile(r"\s*·?\s*\b\d{1,4}\s*/\s*\d{1,4}\b\s*$")

# Words too common to say anything about which script a transcript belongs to.
_COMMON = frozenset("""
que con por para una uno unos unas los las del como pero mas más sus tus mis este esta esto
eso esa ese hay ser son era fue muy sin sobre cuando donde porque también tambien todo toda
""".split())


@dataclass(frozen=True)
class SheetTexts:
    """The texts of one platform, as the sheet gives them (format 2)."""

    title: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    first_comment: str = ""
    # YouTube's search tags (long videos), plain words, not hashtags.
    keywords: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.title or self.description)


@dataclass(frozen=True)
class ReelInfo:
    """What a sheet says about its reel. Every field may be empty."""

    script: str
    hook: str = ""
    title: str = ""
    caption_lines: tuple[str, ...] = ()
    hashtags: tuple[str, ...] = ()
    pins: tuple[str, ...] = ()
    idea: str = ""
    verdict: str = ""
    verdict_notes: str = ""
    raw: str = field(default="", repr=False)
    # Format 2 only.
    version: int = 1
    duration: float | None = None
    video: str = ""
    series: str = ""
    keyword: str = ""
    origin: str = ""
    platforms: dict = field(default_factory=dict, repr=False, compare=False, hash=False)

    @property
    def needs_review(self) -> bool:
        """The generator's fact-checker asked for a human look."""
        v = fold(self.verdict)
        return "revisar" in v or "review" in v or "descart" in v

    @property
    def has_texts(self) -> bool:
        """It brings final texts for at least one platform (format 2)."""
        return any(not t.empty for t in self.platforms.values())

    def texts_for(self, platform: str) -> SheetTexts | None:
        """The texts to publish on ``platform``, ready as they are.

        A sheet with only one platform (a long video has no TikTok section)
        lends it to the other, as «Copiar de TikTok» does in the editor.
        None when the sheet brings no texts at all (format 1).
        """
        own = self.platforms.get(platform)
        if own is not None and not own.empty:
            return own
        other = next((t for p, t in self.platforms.items() if p != platform and not t.empty), None)
        if other is None:
            return None
        return SheetTexts(title=other.title, description=other.description, tags=other.tags,
                          first_comment=other.first_comment)


def _header(line: str) -> str | None:
    match = _HEADER_RE.match(line.strip())
    return fold(match.group(1)) if match else None


def version_of(text: str) -> int:
    """2 (or later) for a format-2 sheet, by its first line; 0 otherwise."""
    first = next((line for line in (text or "").splitlines() if line.strip()), "")
    match = _SIGNATURE_RE.match(first)
    return int(match.group(1)) if match else 0


def is_sheet(text: str) -> bool:
    """True when the text is a reel sheet rather than a caption."""
    if version_of(text) >= 2:
        return True
    headers = {_header(line) for line in (text or "").splitlines()} - {None}
    if headers & _CAPTION_HEADERS:
        return True
    metas = {fold(m.group(1)) for m in (_META_RE.match(line.strip())
                                        for line in (text or "").splitlines()) if m}
    known = metas & (_TITLE_KEYS | _IDEA_KEYS | _VERDICT_KEYS)
    return bool(headers & _PIN_HEADERS) + len(known) >= 2


def parse(text: str) -> ReelInfo | None:
    """Read a sheet; None when the text is not one (then it is a caption)."""
    if not is_sheet(text):
        return None
    version = version_of(text)
    if version >= 2:
        return _parse_v2(text, version)
    lines = [line.rstrip() for line in text.splitlines()]

    # The script is everything before the lone "—" (or before the first
    # section, if someone removed that line).
    cut = next((i for i, line in enumerate(lines) if line.strip() == "—"), None)
    if cut is None:
        cut = next((i for i, line in enumerate(lines)
                    if _header(line) or _META_RE.match(line.strip())), len(lines))
    script = " ".join(line.strip() for line in lines[:cut] if line.strip())

    hook = title = idea = verdict = verdict_notes = ""
    caption_lines: list[str] = []
    hashtags: list[str] = []
    pins: list[str] = []
    section = "head"
    for line in lines[cut + 1:] if cut < len(lines) else []:
        stripped = line.strip()
        if not stripped:
            continue
        header = _header(stripped)
        if header is not None:
            section = ("caption" if header in _CAPTION_HEADERS
                       else "pins" if header in _PIN_HEADERS else "other")
            continue
        meta = _META_RE.match(stripped)
        key = fold(meta.group(1)).strip() if meta else ""
        if meta and key in _TITLE_KEYS:
            title = meta.group(2).strip()
            continue
        if meta and key in _IDEA_KEYS:
            idea, section = meta.group(2).strip(), "tail"
            continue
        if meta and key in _VERDICT_KEYS:
            value = meta.group(2).strip()
            verdict, _, verdict_notes = (part.strip() for part in value.partition("—"))
            verdict = verdict.lstrip("⚠").strip()
            section = "tail"
            continue
        if section == "head" and not hook:
            hook = stripped
        elif section == "caption":
            tags = _TAG_RE.findall(stripped)
            if tags and not _TAG_RE.sub("", stripped).strip():
                hashtags.extend(tags)
            else:
                caption_lines.append(stripped)
        elif section == "pins":
            pins.append(stripped.lstrip("·").strip())

    return ReelInfo(
        script=script, hook=hook, title=title,
        caption_lines=tuple(caption_lines), hashtags=tuple(dict.fromkeys(hashtags)),
        pins=tuple(p for p in pins if p), idea=idea,
        verdict=verdict, verdict_notes=verdict_notes, raw=text,
    )


def parse_duration(text: str) -> float | None:
    """«46,8 s», «46.8 s», «10:23» or «1:02:03» -> seconds; None if unreadable."""
    value = (text or "").strip().lower()
    clock = re.match(r"^(?:(\d+):)?(\d+):(\d{2})(?:[.,](\d+))?", value)
    if clock:
        hours, minutes, seconds = int(clock.group(1) or 0), int(clock.group(2)), int(clock.group(3))
        return float(hours * 3600 + minutes * 60 + seconds)
    number = re.match(r"^(\d+(?:[.,]\d+)?)", value)
    return float(number.group(1).replace(",", ".")) if number else None


def _meta(line: str) -> tuple[str, str] | None:
    match = _META_RE.match(line.strip())
    return (fold(match.group(1)).strip(), match.group(2).strip()) if match else None


def _platform_texts(lines: list[str]) -> SheetTexts:
    """One platform section: «· clave: valor» fields, and blocks after an empty key."""
    fields: dict[str, list[str]] = {}
    key = None
    for line in lines:
        meta = _meta(line)
        if meta and meta[0] in _PLATFORM_KEYS:
            key = _PLATFORM_KEYS[meta[0]]
            fields[key] = [meta[1]] if meta[1] else []
            continue
        if key is not None:
            fields[key].append(line.rstrip())

    def text(name: str) -> str:
        rows = fields.get(name, [])
        while rows and not rows[-1].strip():
            rows = rows[:-1]
        while rows and not rows[0].strip():
            rows = rows[1:]
        return "\n".join(rows).strip()

    keywords = tuple(k.strip() for k in re.split(r"[,\n]+", text("keywords")) if k.strip())
    return SheetTexts(title=" ".join(text("title").split()), description=text("description"),
                      tags=tuple(dict.fromkeys(_TAG_RE.findall(text("tags")))),
                      first_comment=" ".join(text("first_comment").split()), keywords=keywords)


def _parse_v2(text: str, version: int) -> ReelInfo:
    """Format 2: a signature, the head, then «— section —» blocks."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip()) + 1
    head: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in lines[start:]:
        name = _header(line)
        in_script = bool(sections) and sections[-1][0] in _SCRIPT_HEADERS
        if name is not None and (not in_script or name in _AFTER_SCRIPT):
            sections.append((name, []))
        elif sections:
            sections[-1][1].append(line)
        else:
            head.append(line)
    metas: dict[str, str] = {}
    for line in head + [x for name, body in sections if name == "control" for x in body]:
        meta = _meta(line)
        if meta and meta[0] not in metas:
            metas[meta[0]] = meta[1]
    platforms = {name: _platform_texts(body) for name, body in sections if name in PLATFORMS}
    script = " ".join(" ".join(x.split()) for name, body in sections if name in _SCRIPT_HEADERS
                      for x in body if x.strip())
    verdict_raw = next((metas[k] for k in _VERDICT_KEYS if k in metas), "")
    verdict, _, notes = (part.strip() for part in verdict_raw.partition("—"))
    hook = metas.get("gancho") or metas.get("hook") or (re.split(r"(?<=[.!?…])\s", script)[0] if script else "")
    tiktok = platforms.get("tiktok") or SheetTexts()
    pins = [t.first_comment for t in platforms.values() if t.first_comment]
    return ReelInfo(
        script=script, hook=hook, title=next((metas[k] for k in _TITLE_KEYS if k in metas), ""),
        caption_lines=tuple(line for line in tiktok.description.splitlines() if line.strip()),
        hashtags=tiktok.tags, pins=tuple(dict.fromkeys(pins)), idea=metas.get("idea", ""),
        verdict=verdict.lstrip("⚠").strip(), verdict_notes=notes, raw=text, version=version,
        duration=parse_duration(metas.get("duracion", "")), video=metas.get("video", ""),
        series=SERIES_COUNTER_RE.sub("", metas.get("serie", "")).strip(" ·"),
        keyword=metas.get("palabra clave", ""), origin=metas.get("textos", ""), platforms=platforms,
    )


# --------------------------------------------------------------------------- #
# Does the sheet belong to this video?
# --------------------------------------------------------------------------- #

def duration_match(sheet: ReelInfo, seconds: float | None) -> tuple[bool | None, str]:
    """Does the sheet's duration match the video's? ``(answer, note)``.

    Free, unlike listening: ffprobe already measured the video. None when
    either side does not know (a format-1 sheet, a probe that failed).
    """
    if not sheet.duration or not seconds:
        return None, ""
    tolerance = max(DURATION_TOLERANCE, DURATION_TOLERANCE_SHARE * seconds)
    if abs(sheet.duration - seconds) <= tolerance:
        return True, f"ficha .txt del mismo vídeo (dura {_secs(seconds)}, como dice la ficha)"
    return False, (f"el .txt no parece de este vídeo: el vídeo dura {_secs(seconds)} y la ficha dice "
                   f"{_secs(sheet.duration)}")


def _secs(seconds: float) -> str:
    return f"{seconds:.1f}".replace(".", ",") + " s"

def _content_words(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(fold(text)) if len(w) > 2 and w not in _COMMON]


def script_match(script: str, heard: str) -> float | None:
    """Share of what Whisper heard that appears, in order, in the script.

    ``heard`` is the transcript of the first seconds of the video, so it is
    compared with the opening of the script (plus some slack for Whisper
    merging or splitting words). A sheet read aloud by a synthetic voice
    scores above 0.8; another reel of the same series, around 0.1. None
    when there is too little speech to tell.
    """
    heard_words = _content_words(heard)
    if len(heard_words) < 4:
        return None
    script_words = _content_words(script)
    window = script_words[: int(len(heard_words) * 1.6) + 8]
    matcher = SequenceMatcher(None, heard_words, window, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return round(matched / len(heard_words), 2)
