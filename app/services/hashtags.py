"""Choosing hashtags that are valid, on topic, and weighted by what worked.

There is no public, unauthenticated source of TikTok hashtag audiences any
more (the Creative Center endpoint answers "no permission" since 2026), so
"brings an audience" cannot be looked up. It is built from three things:

* **on topic** — words the transcript actually contains, from a local LLM when
  there is one or from plain keyword extraction, plus the categories of a
  small hand-kept bank (``data/hashtags.json``) when the video clearly is
  about one of them;
* **what worked here** — the account's own analytics: a tag used on three or
  more published videos gets scored by the average views of those videos, and
  the ones that clearly underperform are pushed out of the list;
* **reach** — the well-known broad tags of the bank (#parati, #fyp), only if
  ``HASHTAG_REACH`` asks for them: they say nothing about what a video is
  about, and since August 2025 TikTok counts five hashtags at most, so each
  one costs a slot that a topic tag would use better.

The fixed hashtags from Ajustes always go first and are never dropped.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Account, VideoItem, VideoMetric, VideoStatus

log = logging.getLogger("scheduler")

MAX_TAG_LENGTH = 40
# A tag needs this many published videos behind it before its numbers count.
MIN_USES_TO_TRUST = 3
# Below this share of the account's average views a trusted tag is demoted.
UNDERPERFORM_RATIO = 0.5
# TikTok counts the first five hashtags of a post and ignores the rest.
TIKTOK_MAX_TAGS = 5
NICHE_TAGS = 5
CATEGORY_TAGS = 2
# A bank category needs this much evidence before it speaks for the video: one
# passing «móvil» made 30 videos about loneliness «#tecnologia».
MIN_CATEGORY_HITS = 3
MIN_CATEGORY_KEYWORDS = 2

_TAG_RE = re.compile(r"#([^\W_][\w]*)", re.UNICODE)
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# Function words and conversational filler that never make a useful hashtag.
STOPWORDS = frozenset("""
a al algo alguna algunas alguno algunos ante antes aquel aquella aquellas aquellos aqui aquí
asi así aun aún aunque bien bueno cada casi cierto como cómo con contra cosa cosas cual cuál
cuales cuando cuándo cuanto cuánto de del desde despues después donde dónde dos durante e el él
ella ellas ellos en entonces entre era eran eres es esa esas ese eso esos esta está estaba estaban
estamos estan están estar estas este esto estos estoy fue fueron ha habia había haber hace hacer
hago han has hasta hay he hecho hemos hola hoy igual incluso ir la las le les lo los luego mas más
me mejor menos mi mientras mis mismo mucho muchas muchos muy nada nadie ni ningun ningún no nos
nosotros nuestra nuestro nunca o os otra otras otro otros para parte pero poco por porque pues
puede pueden puedes que qué queria quería quien quién quiere quiero se sea ser si sí siempre sin
sino sobre solo sólo somos son soy su sus tal tambien también tan tanto te tengo tener tiene tienen
tienes toda todas todo todos tu tú tus un una unas uno unos usted va vamos van vas vez vosotros voy
y ya yo chicos chicas gente video vídeo videos vídeos tiktok cuenta cuentas dia día dias días
ahora luego entonces bueno vale venga claro ok okay eh em mmm sabes digo decir dice tema temas
manera forma ejemplo ejemplos primero segundo tercero cuarto quinto siguiente ultimo último
cuento contar cuenta os mira mirad fijate fíjate escucha escuchad atencion atención importante
verdad realmente simplemente seguro segura hablar hablo habla hemos hacen haces hicieron
uno dos tres cuatro cinco seis siete ocho nueve diez once doce veinte treinta cincuenta cien mil
minuto minutos hora horas semana semanas mes meses año años vuestra vuestro
""".split())

# Broad tags that name no topic: never counted as a video's own tags.
GENERIC_TAGS = frozenset({
    "parati", "fyp", "fypシ", "foryou", "foryoupage", "viral", "viralvideo", "tiktok",
    "trending", "reels", "xyzbca",
})


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

def fold(text: str, *, keep_enye: bool = False) -> str:
    """Lowercase ASCII-ish form used to compare tags and match keywords.

    ``#hábitos`` and ``#habitos`` are the same tag for us, and a keyword
    written with an accent must still match its unaccented spelling.
    ``keep_enye`` keeps the ñ apart from the n, which is what keyword matching
    needs: folded, «uñas» is «unas», the article, and every «unas» of a
    transcript used to count as a beauty keyword.
    """
    text = text.lower()
    if keep_enye:
        text = text.replace("ñ", "\x00")
    decomposed = unicodedata.normalize("NFKD", text)
    folded = "".join(c for c in decomposed if not unicodedata.combining(c))
    return folded.replace("\x00", "ñ") if keep_enye else folded


def is_latin(char: str) -> bool:
    """Letters of the Latin alphabet (accents, ñ, «1º», «2ª»); non-letters pass too."""
    if not char.isalpha() or char in "ºª":
        return True
    return unicodedata.name(char, "").startswith("LATIN")


def normalize(tag: str) -> str | None:
    """``"#Hábitos!"`` -> ``"#hábitos"``; None when nothing valid is left.

    TikTok allows Unicode letters, digits and underscores, so accents and the
    ñ stay: ``#españa`` is a different (and bigger) tag than ``#espana``. A tag
    with letters of another alphabet is dropped whole: ``#tiempo独自`` is what
    a small model writes when it slips into Chinese, and ``#tiempo`` would be
    a different tag, not a repaired one.
    """
    body = tag.strip().lstrip("#").strip().lower()
    body = "".join(c for c in body if c.isalnum() or c == "_")
    if len(body) < 2 or len(body) > MAX_TAG_LENGTH or body.isdigit():
        return None
    if not all(is_latin(c) for c in body):
        return None
    return f"#{body}"


def normalize_all(tags, *, exclude=()) -> list[str]:
    """Normalise, drop the invalid, and dedupe by folded form, keeping order."""
    seen = {fold(t) for t in exclude}
    out: list[str] = []
    for raw in tags or []:
        tag = normalize(str(raw))
        if tag is None:
            continue
        key = fold(tag)
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def extract(text: str | None) -> list[str]:
    """The hashtags written in a caption, normalised."""
    return normalize_all(_TAG_RE.findall(text or ""))


def with_fixed(tags, fixed) -> list[str]:
    """A video's own hashtags, then the fixed ones of Ajustes it still lacks.

    What the publisher appends under the description: the video's list in its
    order (the generator already put the fixed ones first), plus any fixed tag
    added to Ajustes after the list was written.
    """
    own = list(tags or [])
    known = {fold(t) for t in own}
    return own + [t for t in fixed or [] if fold(t) not in known]


_RAW_TAG_RE = re.compile(r"#[^\s#]+")


def plain_tags(tags, caption: str = "") -> list[str]:
    """The list YouTube gets: no «#», distinct by folded form, in order.

    The hashtags written inside the description count too, after the list.
    """
    out: list[str] = []
    seen: set[str] = set()
    written = [m.lstrip("#") for m in _RAW_TAG_RE.findall(caption or "")]
    for tag in [str(t).lstrip("#").strip() for t in tags or []] + written:
        key = fold(tag)
        if key and key not in seen:
            seen.add(key)
            out.append(tag)
    return out


# --------------------------------------------------------------------------- #
# The bank
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Category:
    name: str
    keywords: tuple[str, ...]
    hashtags: tuple[str, ...]


@dataclass(frozen=True)
class Bank:
    general: tuple[str, ...] = ()
    categories: tuple[Category, ...] = field(default_factory=tuple)


DEFAULT_BANK = Bank(general=("parati", "fyp", "viral"))


def load_bank(path: Path) -> Bank:
    """Read ``data/hashtags.json``; a missing or broken file means no topics."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("hashtag bank unreadable (%s): %s", path, exc)
        return DEFAULT_BANK
    categories = []
    for name, raw in (data.get("categories") or {}).items():
        if not isinstance(raw, dict):
            continue
        categories.append(Category(
            name=str(name),
            keywords=tuple(dict.fromkeys(fold(str(k), keep_enye=True)
                                         for k in raw.get("keywords") or [] if str(k).strip())),
            hashtags=tuple(str(h) for h in raw.get("hashtags") or [] if str(h).strip()),
        ))
    general = tuple(str(g) for g in data.get("general") or [] if str(g).strip())
    return Bank(general=general or DEFAULT_BANK.general, categories=tuple(categories))


def detect_categories(text: str, bank: Bank, *, strict: bool = True) -> list[tuple[str, int]]:
    """Categories the text is clearly about, most hits first.

    ``strict`` (the default) demands :data:`MIN_CATEGORY_HITS` mentions of at
    least :data:`MIN_CATEGORY_KEYWORDS` different keywords. Before that rule,
    87 % of the categories that reached a caption came from one or two words
    said in passing — «casa» made a video #hogar, «cena» made it #receta.
    """
    folded = f" {fold(text, keep_enye=True)} "
    words = Counter(_WORD_RE.findall(folded))
    scored: list[tuple[str, int]] = []
    for cat in bank.categories:
        hits, distinct = 0, 0
        for kw in cat.keywords:
            found = folded.count(f" {kw} ") if " " in kw else words.get(kw, 0)
            hits += found
            distinct += bool(found)
        if not hits:
            continue
        if strict and (hits < MIN_CATEGORY_HITS or distinct < MIN_CATEGORY_KEYWORDS):
            continue
        scored.append((cat.name, hits))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored


def category_tags(text: str, bank: Bank, limit: int = CATEGORY_TAGS) -> list[str]:
    """Tags from the (at most two) categories the text is clearly about, interleaved."""
    matched = detect_categories(text, bank)[:2]
    by_name = {c.name: c for c in bank.categories}
    lanes = [list(by_name[name].hashtags) for name, _ in matched if name in by_name]
    out: list[str] = []
    while lanes and len(out) < limit:
        for lane in list(lanes):
            if not lane:
                lanes.remove(lane)
                continue
            out.append(lane.pop(0))
            if len(out) >= limit:
                break
    return out


# --------------------------------------------------------------------------- #
# Keywords straight from the transcript (the no-LLM path)
# --------------------------------------------------------------------------- #

def keyword_tags(text: str, bank: Bank | None = None, limit: int = NICHE_TAGS) -> list[str]:
    """Most repeated meaningful words, as hashtags. Crude, but on topic.

    Repetition first; then words the bank knows as topic keywords, because a
    word that names a whole category beats one that happens to be long; then
    length, then order of appearance.
    """
    known = {kw for cat in (bank.categories if bank else ()) for kw in cat.keywords}
    counts: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    for i, word in enumerate(_WORD_RE.findall(text.lower())):
        key = fold(word)
        if len(key) < 5 or key in STOPWORDS or word in STOPWORDS:
            continue
        counts[word] += 1
        first_seen.setdefault(word, i)
    ranked = sorted(
        counts,
        key=lambda w: (-counts[w], fold(w, keep_enye=True) not in known, -len(w), first_seen[w]),
    )
    return normalize_all(ranked)[:limit]


# --------------------------------------------------------------------------- #
# What worked on this account
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TagScore:
    uses: int
    views_avg: float


def learned_scores(
    session: Session, account_id: int | None = None, *, platform: str | None = None,
) -> tuple[dict[str, TagScore], float]:
    """Average views per hashtag on this account's measured videos.

    Returns ``({folded tag: score}, account average views)``. Only published
    videos with metrics count, and the tag is whatever the final caption
    carried, so hand-edited hashtags are learned from too.

    ``platform`` pools every account of one platform instead: the library
    writes a video's texts before it knows which account will publish it, so
    what it can learn from is "what worked on TikTok", not on one account.
    """
    stmt = (
        select(VideoItem.rendered_caption, VideoMetric.views)
        .join(VideoMetric, VideoMetric.video_id == VideoItem.id)
        .where(VideoItem.status == VideoStatus.PUBLISHED)
    )
    if account_id is not None:
        stmt = stmt.where(VideoItem.account_id == account_id)
    if platform is not None:
        stmt = stmt.join(Account, Account.id == VideoItem.account_id).where(
            Account.platform == platform)
    rows = session.execute(stmt).all()
    if not rows:
        return {}, 0.0
    totals: dict[str, list[int]] = {}
    for caption, views in rows:
        for tag in extract(caption):
            totals.setdefault(fold(tag), []).append(int(views or 0))
    scores = {tag: TagScore(len(v), sum(v) / len(v)) for tag, v in totals.items()}
    account_avg = sum(int(v or 0) for _, v in rows) / len(rows)
    return scores, account_avg


def rank_by_evidence(tags: list[str], scores: dict[str, TagScore], account_avg: float) -> list[str]:
    """Proven tags first, unknown ones in their given order, weak ones last.

    "Proven" and "weak" both need ``MIN_USES_TO_TRUST`` videos behind the tag:
    a single lucky or unlucky video must not decide anything.
    """
    good, unknown, weak = [], [], []
    for tag in tags:
        score = scores.get(fold(tag))
        if score is None or score.uses < MIN_USES_TO_TRUST:
            unknown.append(tag)
        elif account_avg and score.views_avg < account_avg * UNDERPERFORM_RATIO:
            weak.append(tag)
        else:
            good.append(tag)
    good.sort(key=lambda t: -scores[fold(t)].views_avg)
    return good + unknown + weak


# --------------------------------------------------------------------------- #
# Putting the list together
# --------------------------------------------------------------------------- #

def is_generic(tag: str) -> bool:
    """#fyp, #parati and friends: reach tags, never a video's own topic."""
    return fold(normalize(tag) or "").lstrip("#") in GENERIC_TAGS


def select_tags(
    *,
    fixed: list[str],
    niche: list[str],
    transcript: str,
    bank: Bank,
    scores: dict[str, TagScore] | None = None,
    account_avg: float = 0.0,
    limit: int = TIKTOK_MAX_TAGS,
    reach: int = 0,
    min_topic: int = 3,
) -> list[str]:
    """The hashtags for one video: fixed, then niche, then topic, then reach.

    ``niche`` are the video's own tags (the language model's, or the repeated
    words of the transcript); the bank only fills in when they are fewer than
    ``min_topic``, and only with a category the transcript is clearly about.
    ``reach`` broad tags go last, and only if there is room.
    """
    scores = scores or {}
    fixed_tags = normalize_all(fixed)
    taken = list(fixed_tags)

    def add(group: list[str], cap: int) -> None:
        clean = normalize_all(group, exclude=taken)
        for tag in rank_by_evidence(clean, scores, account_avg)[:cap]:
            if len(taken) >= limit:
                return
            taken.append(tag)

    # Niche words must not just repeat the reach tags in disguise.
    add([t for t in niche if not is_generic(t)], NICHE_TAGS)
    topic = len(taken) - len(fixed_tags)
    if topic < min_topic:
        add(category_tags(transcript, bank), min(CATEGORY_TAGS, min_topic - topic))
    if reach:
        add(list(bank.general), reach)
    return taken
