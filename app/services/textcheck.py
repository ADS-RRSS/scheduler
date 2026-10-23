"""Reviewing a text before it goes out, with the rules the generator uses.

The generator (autocaption.py) checks what the model writes — another
alphabet, English, a stock opener, a first line too long for the space before
«más», a series counter left behind — and hands the problems back to the
model. A text written or corrected by hand deserves the same review, and live:
the editor sends the four texts of one platform here on every pause in the
typing and shows what comes back.

    check()    what is wrong, per field, and what the post will look like
    autofix()  the fixes that need no model («¿¡», «4/50», hashtags in the
               text, English tags, duplicates, #fyp)

Three levels, because they ask for different things: an *error* is refused or
cut by the platform (a title over 100 characters, a caption over 2200), a
*warn* is worth fixing before it goes out, a *tip* is a suggestion.

Nothing here touches the database: the routes pass the context (the fixed
hashtags of Ajustes, what the video says, the series' other descriptions).
"""
from __future__ import annotations

import random
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from zoneinfo import ZoneInfo

from . import autocaption, captions, hashtags

PLATFORMS = ("tiktok", "youtube")

# The first line is what shows before «más»; the generator writes it in 40-100.
FOLD_CHARS = 100
HOOK_MIN_CHARS = 25
TITLE_MAX_CHARS = autocaption.TITLE_MAX_CHARS
TITLE_MIN_CHARS = 20
TIKTOK_CAPTION_MAX = captions.MAX_CAPTION_CHARS
YOUTUBE_DESCRIPTION_MAX = 5000
# YouTube's ceiling for the whole tag list, commas included.
YOUTUBE_TAGS_MAX_CHARS = 500
# TikTok cuts comments (and so the pinned one) at 150 characters.
TIKTOK_COMMENT_MAX = 150
MIN_TOPIC_TAGS = 3
KNOWN_FIELDS = frozenset({"fecha", "hora", "dia", "mes", "indice", "archivo", "hashtags"})

_PLACEHOLDER_RE = re.compile(r"\[([^\[\]\n]{1,30})\]")
_INLINE_TAG_RE = re.compile(r"(?<![\w#])#[^\s#]+")
# A series counter such as «· 7/50» (or «7/50» alone on its line). Stricter
# than the sheet parser's, because a person may end a line with a date:
# «el 3/10» is not a counter, «Serie de hábitos · 3/10» is.
_COUNTER_RE = re.compile(r"(?:^|\s*·\s*)(?<![\d/])([1-9]\d{0,3})\s*/\s*(\d{2,4})(?![\d/])\s*$")

LEVELS = ("error", "warn", "tip")


@dataclass(frozen=True)
class Texts:
    title: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    first_comment: str = ""

    @classmethod
    def of(cls, title="", description="", tags=(), first_comment="") -> "Texts":
        return cls(title=title or "", description=description or "",
                   tags=tuple(str(t) for t in tags or () if str(t).strip()),
                   first_comment=first_comment or "")

    def as_dict(self) -> dict:
        return {"title": self.title, "description": self.description,
                "tags": list(self.tags), "first_comment": self.first_comment}


@dataclass(frozen=True)
class Issue:
    level: str
    field: str
    code: str
    message: str
    fixable: bool = False


@dataclass
class Context:
    """What the review compares a text with. Everything is optional."""

    # The hashtags from Ajustes that every post carries.
    fixed: tuple[str, ...] = ()
    # What the video says (its sheet's script or Whisper's transcript): an
    # English word the video itself uses is no mistake.
    source: str = ""
    # First lines of the series' other descriptions, most recent first.
    siblings: tuple[str, ...] = ()
    file_name: str = ""
    timezone: str = "Europe/Madrid"


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    preview: dict = field(default_factory=dict)

    def add(self, level: str, field_name: str, code: str, message: str, fixable: bool = False):
        self.issues.append(Issue(level, field_name, code, message, fixable))

    def as_dict(self) -> dict:
        order = {level: i for i, level in enumerate(LEVELS)}
        issues = sorted(self.issues, key=lambda i: order.get(i.level, 9))
        return {
            "issues": [asdict(i) for i in issues],
            "counts": {level: sum(i.level == level for i in issues) for level in LEVELS},
            "fixable": any(i.fixable for i in issues),
            "stats": self.stats,
            "preview": self.preview,
        }


# --------------------------------------------------------------------------- #
# Small readers
# --------------------------------------------------------------------------- #

def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def first_line(text: str) -> str:
    lines = _lines(_INLINE_TAG_RE.sub("", text or ""))
    return re.sub(r"\s{2,}", " ", lines[0]).strip() if lines else ""


def inline_tags(text: str) -> list[str]:
    """Hashtags written inside the description, as typed."""
    return _INLINE_TAG_RE.findall(text or "")


def _emojis(text: str) -> list[str]:
    return autocaption._EMOJI_RE.findall(text or "")


def _foreign(text: str) -> str:
    return autocaption._foreign_chars(text or "")


def _english(text: str, source: str) -> list[str]:
    allowed = {w.lower() for w in autocaption._ENGLISH_RE.findall(source or "")}
    return sorted({w.lower() for w in autocaption._ENGLISH_RE.findall(text or "")} - allowed)


def _missing_marks(text: str) -> bool:
    return any(autocaption._open_marks(line) != line for line in _lines(text))


def _english_tag(tag: str) -> str | None:
    """The Spanish tag an English one should be, if there is a clear one."""
    body = hashtags.fold((hashtags.normalize(tag) or "").lstrip("#"))
    spanish = autocaption._ENGLISH_TAGS.get(body)
    return f"#{spanish}" if spanish else None


def _placeholders(text: str) -> list[str]:
    return [m.group(1) for m in _PLACEHOLDER_RE.finditer(text or "")
            if m.group(1).strip().lower() not in KNOWN_FIELDS]


def _braces_balanced(text: str) -> bool:
    depth = 0
    for char in text or "":
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def _counter(line: str) -> re.Match | None:
    match = _COUNTER_RE.search(line or "")
    return match if match and int(match.group(1)) <= int(match.group(2)) else None


# --------------------------------------------------------------------------- #
# What goes out
# --------------------------------------------------------------------------- #

def final_tags(texts: Texts, ctx: Context) -> list[str]:
    """The list the publisher appends: the video's own, then the fixed ones it lacks."""
    return hashtags.with_fixed(hashtags.normalize_all(texts.tags), ctx.fixed)


def render(texts: Texts, platform: str, ctx: Context, now: datetime | None = None) -> dict:
    """What the post will say, the way publisher.prepare() writes it.

    Variants ``{a|b}`` resolve to one example (always the same one, so the
    preview does not flicker while typing) and the fields take today's values.
    """
    tags = final_tags(texts, ctx)
    when = now or datetime.now(ZoneInfo(ctx.timezone))
    caption = captions.render_caption(
        texts.description, hashtags=tags, hashtag_count=len(tags), filename=ctx.file_name,
        now=when, timezone_name=ctx.timezone, rng=random.Random(0), keep_order=True, limit=None,
    )
    title = captions.render_caption(
        texts.title, hashtags=[], filename=ctx.file_name, now=when,
        timezone_name=ctx.timezone, rng=random.Random(0), keep_order=True,
    )
    # TikTok counts the first five distinct hashtags of the post, wherever
    # they are: those written in the text come before the appended list.
    in_order = hashtags.normalize_all(inline_tags(caption))
    counted = in_order[:hashtags.TIKTOK_MAX_TAGS]
    out = {
        "title": title,
        "caption": caption,
        "visible": first_line(caption)[:FOLD_CHARS],
        "tags": tags,
        "counted_tags": counted,
        "ignored_tags": in_order[hashtags.TIKTOK_MAX_TAGS:],
    }
    if platform == "youtube":
        out["youtube_tags"] = hashtags.plain_tags(tags, texts.description)
    return out


# --------------------------------------------------------------------------- #
# The review
# --------------------------------------------------------------------------- #

def check(texts: Texts, platform: str = "tiktok", ctx: Context | None = None) -> Report:
    """Everything worth saying about one platform's texts."""
    ctx = ctx or Context()
    platform = platform if platform in PLATFORMS else "tiktok"
    report = Report()
    preview = render(texts, platform, ctx)
    report.preview = preview
    _check_title(report, texts, platform)
    _check_description(report, texts, platform, ctx, preview)
    _check_tags(report, texts, platform, ctx, preview)
    _check_comment(report, texts, platform)
    hook = first_line(texts.description)
    report.stats = {
        "title_chars": len(texts.title.strip()),
        "title_max": TITLE_MAX_CHARS,
        "description_chars": len(texts.description.strip()),
        "caption_chars": len(preview["caption"]),
        "caption_max": TIKTOK_CAPTION_MAX if platform == "tiktok" else YOUTUBE_DESCRIPTION_MAX,
        "hook_chars": len(hook),
        "fold_chars": FOLD_CHARS,
        "tags": len(preview["tags"]),
        "tags_counted": len(preview["counted_tags"]),
        "tags_max": hashtags.TIKTOK_MAX_TAGS,
        "comment_chars": len(texts.first_comment.strip()),
        "comment_max": TIKTOK_COMMENT_MAX if platform == "tiktok" else None,
        "emojis": len(_emojis(texts.description)),
    }
    if platform == "youtube":
        plain = preview.get("youtube_tags", [])
        report.stats["youtube_tags_chars"] = len(",".join(plain))
        report.stats["youtube_tags_max"] = YOUTUBE_TAGS_MAX_CHARS
    return report


def _check_title(report: Report, texts: Texts, platform: str) -> None:
    title = texts.title.strip()
    if not title:
        if platform == "youtube":
            report.add("error", "title", "title_missing",
                       "YouTube necesita un título: sin él, el Short sale con uno genérico.")
        return
    if len(title) > TITLE_MAX_CHARS:
        report.add("error", "title", "title_long",
                   f"El título tiene {len(title)} caracteres y se cortará en {TITLE_MAX_CHARS}.")
    if _INLINE_TAG_RE.search(title):
        report.add("warn", "title", "title_hashtags",
                   "El título lleva hashtags: van en la lista de hashtags, no aquí.", fixable=True)
    if _missing_marks(title):
        report.add("warn", "title", "title_marks", "Al título le faltan «¿» o «¡» de apertura.",
                   fixable=True)
    foreign = _foreign(title)
    if foreign:
        report.add("error", "title", "title_foreign",
                   f"El título tiene caracteres de otro alfabeto ({foreign[:8]}).")
    if platform == "youtube" and len(title) < TITLE_MIN_CHARS:
        report.add("tip", "title", "title_short",
                   "Un título tan corto dice poco en el buscador: nombra el tema concreto "
                   "(30 a 80 caracteres).")


def _check_description(report: Report, texts: Texts, platform: str, ctx: Context,
                       preview: dict) -> None:
    text = texts.description
    body = _INLINE_TAG_RE.sub("", text)
    if not body.strip():
        report.add("warn" if platform == "tiktok" else "tip", "description", "desc_empty",
                   "Sin descripción: sólo saldrán los hashtags, y la descripción es lo "
                   "que TikTok lee para el buscador." if platform == "tiktok" else
                   "Sin descripción: YouTube la enseña bajo el título y la usa para buscar.")
        return
    foreign = _foreign(text)
    if foreign:
        report.add("error", "description", "desc_foreign",
                   f"Hay caracteres de otro alfabeto ({foreign[:8]}): así lo escriben los "
                   "modelos cuando se les escapa el chino.")
    english = _english(body, ctx.source)
    if english:
        report.add("warn", "description", "desc_english",
                   "Palabras en inglés que el vídeo no dice: " + ", ".join(english[:6]) + ".")
    hook = first_line(text)
    if hook:
        if autocaption._STOCK_OPENER_RE.search(hook):
            report.add("warn", "description", "hook_stock",
                       "La primera línea arranca con una fórmula gastada («¿Sabías que…?», "
                       "«Descubre…»): nombra el tema concreto.")
        if len(hook) > FOLD_CHARS:
            report.add("warn", "description", "hook_long",
                       f"La primera línea tiene {len(hook)} caracteres: TikTok corta hacia los "
                       f"{FOLD_CHARS} y el resto queda tras «más».")
        elif len(hook) < HOOK_MIN_CHARS:
            report.add("tip", "description", "hook_short",
                       "La primera línea dice poco para el buscador: nombra el tema con las "
                       "palabras que alguien escribiría para encontrarlo.")
        for sibling in ctx.siblings:
            if sibling and autocaption._same_opening(hook, sibling):
                report.add("tip", "description", "same_opening",
                           f"Arranca igual que otro vídeo de la serie («{sibling[:60]}»): "
                           "cambia el arranque para que no parezcan plantilla.")
                break
    if _missing_marks(body):
        report.add("warn", "description", "open_marks",
                   "Faltan «¿» o «¡» de apertura.", fixable=True)
    if any(_counter(line) for line in _lines(body)):
        report.add("warn", "description", "series_counter",
                   "Queda un contador de serie («4/50»): es de uso interno, no para quien lo ve.",
                   fixable=True)
    if "·" in body and len(re.findall(r"\s·\s", body)) >= 2:
        report.add("warn", "description", "keyword_list",
                   "Parece una lista de palabras clave separadas por «·»: escribe frases, que "
                   "es lo que TikTok entiende.")
    if len(_emojis(text)) > 1:
        report.add("tip", "description", "emoji_many",
                   f"{len(_emojis(text))} emojis: con uno basta, y sólo si aporta.")
    if not any(line.rstrip().endswith("?") for line in _lines(body)):
        report.add("tip", "description", "no_question",
                   "Sin pregunta: una pregunta al final invita a comentar.")
    unknown = _placeholders(text)
    if unknown:
        report.add("warn", "description", "placeholder_unknown",
                   "Campos que no existen y saldrán tal cual: "
                   + ", ".join(f"[{u}]" for u in unknown[:4])
                   + ". Los que hay: [fecha] [hora] [dia] [mes] [archivo] [hashtags].")
    if not _braces_balanced(text):
        report.add("warn", "description", "spintax_unbalanced",
                   "Las llaves de las variantes {a|b} no están cerradas: saldrán tal cual.")
    own = {hashtags.fold(t) for t in hashtags.normalize_all(texts.tags)}
    repeated = [t for t in hashtags.normalize_all(inline_tags(text)) if hashtags.fold(t) in own]
    if repeated:
        report.add("warn", "description", "inline_tag_duplicate",
                   f"{' '.join(repeated)} {_plural(len(repeated), 'está', 'están')} en el texto y "
                   "en la lista: saldría dos veces.", fixable=True)
    limit = TIKTOK_CAPTION_MAX if platform == "tiktok" else YOUTUBE_DESCRIPTION_MAX
    if len(preview["caption"]) > limit:
        report.add("error", "description", "caption_long",
                   f"Con los hashtags son {len(preview['caption'])} caracteres y el máximo es "
                   f"{limit}: se cortará.")


def _check_tags(report: Report, texts: Texts, platform: str, ctx: Context, preview: dict) -> None:
    raw = [t for t in texts.tags if str(t).strip()]
    foreign = [t for t in raw if _foreign(t)]
    invalid = [t for t in raw if t not in foreign and hashtags.normalize(t) is None]
    if foreign:
        report.add("error", "tags", "tag_foreign",
                   "Hashtags con letras de otro alfabeto, que se quitarán: " + " ".join(foreign[:5])
                   + ".", fixable=True)
    if invalid:
        report.add("warn", "tags", "tag_invalid",
                   "No " + _plural(len(invalid), "es un hashtag válido y se quitará",
                                   "son hashtags válidos y se quitarán")
                   + ": " + " ".join(invalid[:6]) + " (letras, números y «_», de 2 a 40).",
                   fixable=True)
    valid = [hashtags.normalize(t) for t in raw if hashtags.normalize(t)]
    if len(valid) != len(hashtags.normalize_all(valid)):
        report.add("warn", "tags", "tag_duplicate", "Hay hashtags repetidos.", fixable=True)
    generic = list(dict.fromkeys(t for t in valid if hashtags.is_generic(t)))
    if generic:
        report.add("tip", "tags", "tag_generic",
                   f"{' '.join(generic)} no {_plural(len(generic), 'dice', 'dicen')} de qué va "
                   "el vídeo y ocupa un hueco de los cinco que cuenta TikTok.", fixable=True)
    english = [(t, _english_tag(t)) for t in valid if _english_tag(t)]
    if english:
        report.add("warn", "tags", "tag_english",
                   "Hashtags en inglés con equivalente claro: "
                   + ", ".join(f"{t} → {s}" for t, s in english[:5]) + ".", fixable=True)
    if platform == "tiktok" and preview["ignored_tags"]:
        report.add("warn", "tags", "tags_over_limit",
                   f"TikTok sólo cuenta los {hashtags.TIKTOK_MAX_TAGS} primeros: "
                   + " ".join(preview["ignored_tags"]) + " no cuentan"
                   + (" (los fijos de Ajustes también suman)." if ctx.fixed else "."))
    fixed = {hashtags.fold(f) for f in ctx.fixed}
    topic = [t for t in hashtags.normalize_all(valid)
             if not hashtags.is_generic(t) and hashtags.fold(t) not in fixed]
    if len(topic) < MIN_TOPIC_TAGS:
        report.add("tip", "tags", "tags_few",
                   f"{len(topic)} {_plural(len(topic), 'hashtag', 'hashtags')} del tema: entre 3 y 5 "
                   "concretos de este vídeo ayudan a que lo encuentre quien busca eso.")
    if platform == "youtube":
        chars = len(",".join(preview.get("youtube_tags", [])))
        if chars > YOUTUBE_TAGS_MAX_CHARS:
            report.add("warn", "tags", "youtube_tags_long",
                       f"Las etiquetas suman {chars} caracteres y YouTube admite "
                       f"{YOUTUBE_TAGS_MAX_CHARS}: sobran las últimas.")


def _check_comment(report: Report, texts: Texts, platform: str) -> None:
    comment = texts.first_comment.strip()
    if not comment:
        return
    if platform == "tiktok" and len(comment) > TIKTOK_COMMENT_MAX:
        report.add("warn", "first_comment", "comment_long",
                   f"El comentario fijado tiene {len(comment)} caracteres: TikTok lo corta en "
                   f"{TIKTOK_COMMENT_MAX}.")
    foreign = _foreign(comment)
    if foreign:
        report.add("error", "first_comment", "comment_foreign",
                   f"El comentario tiene caracteres de otro alfabeto ({foreign[:8]}).")


# --------------------------------------------------------------------------- #
# The fixes that need no model
# --------------------------------------------------------------------------- #

def _fix_tag(tag: str) -> str | None:
    clean = hashtags.normalize(tag)
    if clean is None:
        return None
    return _english_tag(clean) or clean


def autofix(texts: Texts, platform: str = "tiktok", ctx: Context | None = None
            ) -> tuple[Texts, list[str]]:
    """Apply what can be fixed without a model. Returns the texts and what changed.

    Never rewrites a sentence: it adds the «¿¡» a question is missing, drops a
    series counter, moves the hashtags typed inside the text to the list (where
    the publisher puts them on their own line), and cleans the list — invalid,
    repeated, English with a Spanish equivalent, #fyp and friends.
    """
    changes: list[str] = []

    # Description, line by line, keeping the blank lines the user chose.
    moved: list[str] = []
    lines_out: list[str] = []
    counters = marks = 0
    for line in (texts.description or "").splitlines():
        found = inline_tags(line)
        if found:
            moved.extend(found)
            line = _INLINE_TAG_RE.sub("", line)
        stripped = line.strip()
        counter = _counter(stripped)
        if counter:
            stripped = stripped[:counter.start()].rstrip(" ·")
            counters += 1
        if stripped:
            opened = autocaption._open_marks(stripped)
            if opened != stripped:
                marks += 1
                stripped = opened
        lines_out.append(re.sub(r"[ \t]{2,}", " ", stripped))
    description = re.sub(r"\n{3,}", "\n\n", "\n".join(lines_out)).strip()
    if marks:
        changes.append("Añadidos los «¿» o «¡» de apertura que faltaban.")
    if counters:
        changes.append("Quitado el contador de serie.")
    if moved:
        changes.append(f"{len(moved)} "
                       + _plural(len(moved), "hashtag del texto pasa", "hashtags del texto pasan")
                       + " a la lista.")

    # Title: no hashtags, and its «¿¡».
    title = texts.title
    if _INLINE_TAG_RE.search(title):
        title = re.sub(r"\s{2,}", " ", _INLINE_TAG_RE.sub("", title)).strip()
        changes.append("Quitados los hashtags del título.")
    opened = autocaption._open_marks(title.strip()) if title.strip() else title
    if opened != title.strip() and title.strip():
        title = opened
        changes.append("Añadidos los «¿» o «¡» del título.")

    # The list: what was typed in the text first, since it came first in the post.
    tags: list[str] = []
    seen: set[str] = set()
    dropped, translated, generic, repeated = [], [], [], 0
    for raw in moved + list(texts.tags):
        clean = hashtags.normalize(raw)
        if clean is None:
            dropped.append(raw)
            continue
        spanish = _english_tag(clean)
        if spanish:
            translated.append(f"{clean} → {spanish}")
            clean = spanish
        if hashtags.is_generic(clean):
            generic.append(clean)
            continue
        key = hashtags.fold(clean)
        if key in seen:
            repeated += 1
            continue
        seen.add(key)
        tags.append(clean)
    if dropped:
        changes.append("Quitados los hashtags no válidos: " + " ".join(dropped[:5]) + ".")
    if translated:
        changes.append("En español: " + ", ".join(dict.fromkeys(translated)) + ".")
    if generic:
        names = list(dict.fromkeys(generic))
        changes.append(_plural(len(names), "Quitado ", "Quitados ") + " ".join(names)
                       + _plural(len(names), ": no dice de qué va.", ": no dicen de qué va."))
    if repeated:
        changes.append("Quitados los hashtags repetidos.")

    return replace(texts, title=title, description=description, tags=tuple(tags)), changes


# --------------------------------------------------------------------------- #
# Hashtag suggestions
# --------------------------------------------------------------------------- #

def suggestions(*, current: list[str], fixed: list[str], series: list[tuple[str, int]],
                proven: list[dict], topic: list[str], per_source: int = 8) -> list[dict]:
    """Tags worth offering in the editor, with why, without what is already there.

    ``proven`` are the platform's measured tags (analytics.by_hashtag rows;
    only the trusted ones count), ``series`` the tags of the folder's other
    videos with how many carry them, ``topic`` the bank's categories the
    video is about. Proven first: they are the only ones with numbers behind.
    """
    taken = {hashtags.fold(t) for t in list(current) + list(fixed)}
    out: list[dict] = []

    def offer(candidates, source: str) -> None:
        added = 0
        for tag, detail in candidates:
            clean = hashtags.normalize(tag)
            if clean is None or hashtags.is_generic(clean) or hashtags.fold(clean) in taken:
                continue
            taken.add(hashtags.fold(clean))
            out.append({"tag": clean, "source": source, "detail": detail})
            added += 1
            if added >= per_source:
                return

    offer(((row["tag"], f"{row['views_avg']:,} reproducciones de media en {row['posts']} vídeos"
            .replace(",", ".")) for row in proven if row.get("trusted")), "funcionan")
    offer(((tag, f"en {uses} {_plural(uses, 'vídeo', 'vídeos')} de esta carpeta")
           for tag, uses in series), "serie")
    offer(((tag, "del banco de temas: el vídeo trata de esto") for tag in topic), "tema")
    return out


def sibling_openings(descriptions: list[str], limit: int = autocaption.RECENT_CAPTIONS
                     ) -> tuple[str, ...]:
    """First lines of the series' other descriptions, for the same-opening check."""
    out = []
    for text in descriptions:
        line = first_line(text)
        if line:
            out.append(line)
        if len(out) >= limit:
            break
    return tuple(out)

