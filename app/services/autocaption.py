"""Writing a video's description and hashtags.

    a .txt sheet next to the video (see reelinfo.py)
        -> Whisper listens to the first seconds: is the sheet about this video?
        -> Ollama writes the description from the sheet's script, title,
           draft caption, hashtags and questions
    no sheet, and ffprobe says there is audio
        -> Whisper (local) transcribes it
        -> Ollama (local, optional) writes the description and topic tags
           | or, without Ollama, the opening line becomes the caption and
           | the repeated words become the tags
    -> checks (other alphabets, English, stock openers, repeats…), one editing
       pass that hands the model its own problems, a deterministic tidy-up
    -> hashtags.select_tags() assembles the final list

The description has three lines — a hook with the words people search for, a
concrete fact from the video, a question to comment on — because the first
line is all a viewer sees before «más», and because TikTok reads the caption
as a search signal: a bare 40-character teaser gave it nothing to index.

Everything runs on this machine. Whisper is faster-whisper (CTranslate2) with
its weights downloaded once into ``data/models/whisper``; Ollama is whatever
the user already runs at ``OLLAMA_URL``. Nothing here touches the database:
:mod:`app.scheduler.captioner` decides what to store and when.

All of it is synchronous on purpose: transcription is CPU-bound and the
captioner runs :func:`generate` in a worker thread.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path

import httpx

from ..config import Settings
from . import hashtags as hashtags_svc
from . import reelinfo
from .reelinfo import ReelInfo

log = logging.getLogger("scheduler")

# The no-LLM caption (the video's opening line) and the three lines of a
# written description.
CAPTION_MAX_CHARS = 150
# YouTube rejects titles over 100 characters; Shorts cut them far sooner.
TITLE_MAX_CHARS = 100
HOOK_MAX_CHARS = 110
FACT_MAX_CHARS = 180
QUESTION_MAX_CHARS = 110
MIN_WORDS_FOR_SPEECH = 3
PROBE_TTL_SECONDS = 60
# Below this share of heard words found in the sheet's script, the sheet is
# about another video (a matching one scores ~0.9, a sibling reel ~0.1).
SHEET_MATCH_MIN = 0.5
# Recent descriptions of the account shown to the model so it does not open
# every video of a series the same way.
RECENT_CAPTIONS = 8

# Whisper "hears" these in music and silence. Segments matching them are
# noise, whatever the model's confidence says.
_HALLUCINATIONS = (
    "subtitulos realizados por", "subtitulado por", "amara.org", "subtitles by",
    "gracias por ver el video", "suscribete a mi canal", "thank you for watching",
)
_LEADING_FILLER = re.compile(
    r"^(?:(?:hola|buenas|buenos dias|buenas tardes|hey|ey)[\s,!.]*(?:a todos|chicos|chicas|gente|familia)?[\s,!.]*"
    r"|(?:bueno|pues|vale|venga|mira|mirad|a ver)[\s,]+)+",
    re.IGNORECASE,
)
_FILLER_TOKENS = re.compile(r"\b(?:eh+|em+|ehm|mm+|hmm+)\b[,.]?\s*", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


# --------------------------------------------------------------------------- #
# Whisper
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Transcript:
    text: str = ""
    language: str | None = None
    duration: float = 0.0
    dropped_segments: int = 0

    @property
    def words(self) -> int:
        return len(self.text.split())

    @property
    def has_speech(self) -> bool:
        return self.words >= MIN_WORDS_FOR_SPEECH


_model = None
_model_key: tuple | None = None
_model_lock = threading.Lock()


def whisper_available() -> tuple[bool, str]:
    """Whether faster-whisper can be imported, and why not if it cannot."""
    try:
        import faster_whisper  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - shown to the user
        return False, f"faster-whisper no está instalado ({exc}). Ejecuta: pip install -r requirements.txt"
    return True, ""


def model_cached(settings: Settings) -> bool:
    """True once the weights for the configured model are on disk."""
    root = Path(settings.whisper_models_dir)
    if not root.exists():
        return False
    needle = settings.whisper_model.lower()
    return any(needle in p.name.lower() for p in root.iterdir())


def _load_model(settings: Settings):
    """Load faster-whisper once; the same weights serve every video."""
    global _model, _model_key
    key = (settings.whisper_model, settings.whisper_device, settings.whisper_compute_type)
    with _model_lock:
        if _model is not None and _model_key == key:
            return _model
        from faster_whisper import WhisperModel

        Path(settings.whisper_models_dir).mkdir(parents=True, exist_ok=True)
        import os
        threads = max(1, min(8, (os.cpu_count() or 4) - 2))
        started = time.monotonic()
        _model = WhisperModel(
            settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            download_root=str(settings.whisper_models_dir),
            cpu_threads=threads,
        )
        _model_key = key
        log.info("whisper %s loaded in %.1fs (%s, %s)", settings.whisper_model,
                 time.monotonic() - started, settings.whisper_device,
                 settings.whisper_compute_type)
        return _model


def _is_hallucination(text: str) -> bool:
    folded = hashtags_svc.fold(text)
    return any(marker in folded for marker in _HALLUCINATIONS)


def transcribe(path: Path, settings: Settings, *, max_seconds: float | None = None) -> Transcript:
    """Run Whisper on the file's audio track. Blocking; call from a thread.

    ``max_seconds`` listens to the opening only: enough to recognise a
    script, a fraction of the time of the whole video.

    faster-whisper already drops the segments it takes for silence (high
    no-speech probability *and* low confidence, its own two-part rule), so
    what arrives here is speech as far as the model can tell. The only extra
    filter is for the phrases Whisper invents over music, which it reports
    with full confidence.
    """
    model = _load_model(settings)
    source = str(path)
    if max_seconds:
        from faster_whisper import decode_audio

        rate = 16000
        source = decode_audio(str(path), sampling_rate=rate)[: int(max_seconds * rate)]
    segments, info = model.transcribe(
        source,
        language=settings.whisper_language or None,
        vad_filter=True,
        beam_size=5,
        condition_on_previous_text=False,
    )
    kept: list[str] = []
    dropped = 0
    for segment in segments:
        text = (segment.text or "").strip()
        if not text:
            continue
        if _is_hallucination(text):
            dropped += 1
            continue
        kept.append(text)
    return Transcript(
        text=" ".join(kept).strip(),
        language=getattr(info, "language", None),
        duration=float(getattr(info, "duration", 0.0) or 0.0),
        dropped_segments=dropped,
    )


# --------------------------------------------------------------------------- #
# Ollama: which model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class OllamaStatus:
    reachable: bool
    model: str | None = None
    models: tuple[str, ...] = ()
    error: str = ""


_probe_cache: dict[str, tuple[float, OllamaStatus]] = {}

# Measured on the texts this tool wrote: qwen2.5:7b slipped into Chinese
# («#tiempo独自») and English («¿Lo needs?») and opened a quarter of them with
# the same «¿…? 🤔»; Gemma writes natural Spanish at the same speed on Apple
# Silicon (gemma4:26b is a mixture of experts, ~27 tok/s on an M2 Pro).
_PREFERRED_FAMILIES = ("gemma4", "gemma3")
_MIN_MODEL_BYTES = 4_000_000_000
_EMBEDDING_FAMILIES = {"bert", "nomic-bert", "xlm-roberta", "roberta"}


def reset_probe_cache() -> None:
    _probe_cache.clear()


def _family(model: dict) -> str:
    return str((model.get("details") or {}).get("family") or "").lower()


def _is_chat_model(model: dict) -> bool:
    """Embedding models (embeddinggemma, nomic-embed…) cannot write a caption."""
    name = str(model.get("name") or model.get("model") or "").lower()
    return ("embed" not in name and not name.startswith(("bge-", "all-minilm"))
            and _family(model) not in _EMBEDDING_FAMILIES)


def pick_model(models: list[dict]) -> str | None:
    """The model used when ``OLLAMA_MODEL`` is empty.

    The smallest one big enough to write decent Spanish (4 GB, a 7B model),
    a Gemma first; only when every model is smaller, the largest of them. A
    1B model answers in a second, but a three-line description that has to
    judge a draft caption is beyond it.
    """
    chat = [m for m in models if _is_chat_model(m)]
    if not chat:
        return None

    def size(m: dict) -> int:
        return int(m.get("size") or 0)

    big_enough = [m for m in chat if size(m) >= _MIN_MODEL_BYTES]
    for family in _PREFERRED_FAMILIES:
        fitting = [m for m in big_enough if _family(m) == family
                   or str(m.get("name") or "").lower().startswith(family)]
        if fitting:
            return str(min(fitting, key=size).get("name"))
    if big_enough:
        return str(min(big_enough, key=size).get("name"))
    return str(max(chat, key=size).get("name"))


def probe_ollama(settings: Settings, *, force: bool = False) -> OllamaStatus:
    """Is Ollama there, and which model will be used? Cached for a minute."""
    url = (settings.ollama_url or "").rstrip("/")
    if not url:
        return OllamaStatus(False, error="OLLAMA_URL vacío: desactivado")
    cached = _probe_cache.get(url)
    if cached and not force and time.monotonic() - cached[0] < PROBE_TTL_SECONDS:
        return cached[1]
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get(f"{url}/api/tags")
        resp.raise_for_status()
        models = [m for m in (resp.json().get("models") or []) if isinstance(m, dict)]
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        status = OllamaStatus(False, error=f"Ollama no responde en {url}: {exc}")
        _probe_cache[url] = (time.monotonic(), status)
        return status
    names = tuple(str(m.get("name") or m.get("model") or "") for m in models)
    chosen: str | None = None
    if settings.ollama_model:
        chosen = settings.ollama_model
        if names and not any(n == chosen or n.split(":")[0] == chosen for n in names):
            status = OllamaStatus(False, models=names,
                                  error=f"el modelo «{chosen}» no está en Ollama; hay: "
                                        + ", ".join(names))
            _probe_cache[url] = (time.monotonic(), status)
            return status
    elif models:
        chosen = pick_model(models)
    if not chosen:
        status = OllamaStatus(False, models=names, error="Ollama no tiene ningún modelo de texto instalado")
    else:
        status = OllamaStatus(True, model=chosen, models=names)
    _probe_cache[url] = (time.monotonic(), status)
    return status


# --------------------------------------------------------------------------- #
# The description
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Draft:
    """A description as the model wrote it, line by line."""

    title: str = ""
    hook: str = ""
    fact: str = ""
    question: str = ""
    first_comment: str = ""
    hashtags: tuple[str, ...] = ()
    # What the model changed from a sheet's draft caption, in its words.
    changes: tuple[str, ...] = ()

    @property
    def lines(self) -> list[str]:
        return [line for line in (self.hook, self.fact, self.question) if line]

    @property
    def caption(self) -> str:
        return "\n".join(self.lines)

    def as_json(self) -> str:
        body = {"titulo": self.title, "gancho": self.hook, "dato": self.fact, "pregunta": self.question,
                "primer_comentario": self.first_comment, "hashtags": [t.lstrip("#") for t in self.hashtags]}
        if self.changes:
            body["cambios"] = list(self.changes)
        return json.dumps(body, ensure_ascii=False)


_DESCRIPTION = """LA DESCRIPCIÓN TIENE TRES PARTES
- "gancho": una frase de 40 a 100 caracteres. Es lo único que se lee sin pulsar «más». Nombra el tema concreto con las palabras que alguien escribiría en el buscador de TikTok y da una razón para ver el vídeo. No copies la primera frase del vídeo: ya se oye. Varía el arranque de un vídeo a otro: el dato, el autor, la situación concreta, una pregunta, «Por qué…», «Cómo…».
- "dato": una frase de 60 a 170 caracteres con algo concreto que el vídeo cuenta y que aporte por sí solo: el hallazgo, el autor o el estudio si se nombran, el consejo práctico. Nada que el vídeo no diga.
- "pregunta": una pregunta corta (máximo 100 caracteres) sobre la experiencia de quien lo ve, para que comente.

CÓMO SE ESCRIBE
- Español natural de España, con tildes y con «¿» y «¡» de apertura. Ni una palabra en inglés ni en otro alfabeto, salvo nombres propios que salgan en el vídeo.
- De tú, sin primera persona. Como mucho un emoji en toda la descripción, y sólo si aporta.
- Prohibido: «¿Sabías que…?», «Descubre…», «Secretos de…», «No te lo vas a creer», «Te sorprenderá», promesas vacías, listas de palabras clave separadas por «·» y hashtags dentro del texto."""

_TITLE = """TÍTULO
- "titulo": el título del vídeo en YouTube Shorts, de 30 a 80 caracteres. Nombra el tema concreto con las palabras que alguien escribiría en el buscador y da ganas de verlo. Distinto del gancho. Sin hashtags, sin comillas, sin emojis y sin «#shorts»."""

_TAG_RULES = """- En español, en minúsculas, sin «#» y sin espacios (palabras pegadas: saludmental). Mezcla 1 o 2 amplios de la categoría (psicologia, filosofia) con 2 o 3 concretos de este vídeo (el concepto, la técnica; el autor sólo si es muy conocido y sabes cómo se escribe su nombre).
- Sólo palabras que la gente busca de verdad; nunca una etiqueta hecha con una palabra mal reconocida.
- Nunca genéricos de alcance: fyp, parati, foryou, viral, tiktok."""

TRANSCRIPT_SYSTEM = f"""Eres editor de publicaciones de TikTok en español. Escribes la descripción que acompaña a un vídeo corto a partir de su transcripción automática. Respondes únicamente con un objeto JSON válido, sin explicaciones.

{_DESCRIPTION}
- La transcripción puede tener palabras mal reconocidas: entiende lo que se quiso decir y no repitas los errores.

{_TITLE}

HASHTAGS
- "hashtags": de 3 a 5 sobre el tema concreto de este vídeo.
{_TAG_RULES}

FORMATO
{{"titulo": "…", "gancho": "…", "dato": "…", "pregunta": "…", "hashtags": ["…", "…", "…"]}}"""

SHEET_SYSTEM = f"""Eres editor de publicaciones de TikTok en español. Recibes la ficha de un reel ya producido —el guion completo tal como se oye, el título de la portada, un pie de publicación provisional (o los textos de TikTok y YouTube que trae ya escritos) con sus hashtags y preguntas para el comentario fijado— y escribes la descripción definitiva. Respondes únicamente con un objeto JSON válido, sin explicaciones.

VALORA EL PIE PROVISIONAL
- Quédate con lo que sirve: el tema, las palabras clave que la gente busca y los hashtags que describen ESTE vídeo.
- Quita lo que sobra: contadores de serie como «4/50», la descripción larga de la serie, las listas de palabras clave separadas por «·» y los hashtags que son de otro vídeo de la serie.
- Añade valor sólo con lo que dice el guion: el dato concreto, el autor o el estudio, el consejo práctico.

{_DESCRIPTION}
- Para la pregunta puedes partir de una de las del comentario fijado.

{_TITLE}
- Puedes partir del título de la portada.

COMENTARIO FIJADO
- "primer_comentario": escribe un comentario corto para fijar en el vídeo basándote en las preguntas del comentario fijado de la ficha.

HASHTAGS
- "hashtags": de 3 a 5. Parte de los del pie provisional y conserva los que describen este vídeo; cambia los que no por otros concretos de este guion.
{_TAG_RULES}

"cambios": hasta 3 frases muy cortas con lo que has quitado o añadido respecto al pie provisional.

FORMATO
{{"titulo": "…", "gancho": "…", "dato": "…", "pregunta": "…", "primer_comentario": "…", "hashtags": ["…", "…", "…"], "cambios": ["…"]}}"""

# Ollama turns a JSON schema into a grammar: the keys cannot come back renamed
# («ganchito») and the lists cannot come back as prose.
_DRAFT_PROPERTIES = {
    "titulo": {"type": "string"},
    "gancho": {"type": "string"},
    "dato": {"type": "string"},
    "pregunta": {"type": "string"},
    "primer_comentario": {"type": "string"},
    "hashtags": {"type": "array", "items": {"type": "string"}},
}
TRANSCRIPT_SCHEMA = {"type": "object", "properties": _DRAFT_PROPERTIES,
                     "required": ["titulo", "gancho", "dato", "pregunta", "hashtags"]}
SHEET_SCHEMA = {"type": "object",
                "properties": {**_DRAFT_PROPERTIES,
                               "cambios": {"type": "array", "items": {"type": "string"}}},
                "required": ["titulo", "gancho", "dato", "pregunta", "primer_comentario", "hashtags", "cambios"]}


def topic_from_filename(name: str) -> str:
    """What a descriptive file name says: ``reel_07_serie_tema.mp4`` -> "serie · tema".

    Empty for names that say nothing (``IMG_2034``, a bare number, a hash).
    """
    stem = Path(name or "").stem
    stem = re.sub(r"^(?:reel|video|vid|clip|tiktok)?[_\-\s]*\d+[_\-\s]+", "", stem, flags=re.I)
    if re.match(r"^(?:img|vid|mov|dsc|pxl|screen|grabacion|recording)[_\-\s]", stem, re.I):
        return ""
    parts = [re.sub(r"[-\s]+", " ", p).strip() for p in re.split(r"_+", stem)]
    parts = [p for p in parts if p]
    words = [w for w in _WORD_RE.findall(" ".join(parts)) if len(w) > 2]
    return " · ".join(parts) if len(words) >= 2 else ""


def _recent_block(recent: Sequence[str]) -> str:
    firsts = [c.strip().splitlines()[0] for c in recent if c and c.strip()]
    if not firsts:
        return ""
    return ("\n\nDescripciones recientes de esta cuenta. No repitas sus arranques ni su "
            "estructura:\n" + "\n".join(f"- {line}" for line in firsts[:RECENT_CAPTIONS]))


def transcript_prompt(transcript: Transcript, ctx: Context) -> str:
    parts = ["Transcripción automática del vídeo:\n«" + transcript.text[:3000] + "»"]
    topic = topic_from_filename(ctx.file_name)
    if topic:
        parts.append(f"Nombre del archivo (puede decir la serie y el tema): «{topic}»")
    if ctx.queue_name:
        parts.append(f"Cola: «{ctx.queue_name}»")
    hints = hashtags_svc.category_tags(transcript.text, ctx.bank, limit=6)
    if hints:
        parts.append("Hashtags de un banco genérico que quizá encajen (sólo si el vídeo trata "
                     "de verdad de eso): " + ", ".join(hints))
    return "\n\n".join(parts) + _recent_block(ctx.recent_captions)


def sheet_prompt(sheet: ReelInfo, ctx: Context) -> str:
    parts = []
    if sheet.title:
        parts.append(f"Título de la portada: «{sheet.title}»")
    if sheet.keyword:
        parts.append(f"Lo que la gente busca para encontrarlo: «{sheet.keyword}»")
    if sheet.hook:
        parts.append(f"Primera frase del vídeo: «{sheet.hook}»")
    parts.append("Guion completo:\n«" + sheet.script[:4000] + "»")
    if sheet.has_texts:
        # Format 2: the texts ContentBuilder wrote for each platform are the draft.
        for platform, label in (("tiktok", "TikTok"), ("youtube", "YouTube")):
            texts = sheet.platforms.get(platform)
            if texts is None or texts.empty:
                continue
            parts.append(f"Textos provisionales de {label}:\nTítulo: {texts.title}\n"
                         f"Descripción:\n{texts.description}\nHashtags: {' '.join(texts.tags)}\n"
                         f"Comentario fijado: {texts.first_comment}")
        return "\n\n".join(parts) + _recent_block(ctx.recent_captions)
    draft = "\n".join(sheet.caption_lines)
    parts.append("Pie de publicación provisional:\n" + (draft or "(vacío)"))
    if sheet.hashtags:
        parts.append("Hashtags provisionales: " + " ".join(sheet.hashtags))
    if sheet.pins:
        parts.append("Preguntas para el comentario fijado:\n"
                     + "\n".join(f"- {p}" for p in sheet.pins))
    return "\n\n".join(parts) + _recent_block(ctx.recent_captions)


def _loads(content: str) -> dict | None:
    if not content:
        return None
    try:
        data = json.loads(content)
    except ValueError:
        match = _JSON_BLOCK.search(content)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _list(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[\s,#]+", value)
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def parse_draft(content: str) -> Draft | None:
    """The JSON the model was asked for, tolerating fences, chatter and the
    old single-line shape (``{"caption": …}``). None without a hook."""
    data = _loads(content)
    if data is None:
        return None
    hook = _text(data.get("gancho")) or _text(data.get("hook")) or _text(data.get("caption"))
    if not hook:
        return None
    return Draft(
        title=_text(data.get("titulo")) or _text(data.get("title")),
        hook=hook,
        fact=_text(data.get("dato")) or _text(data.get("fact")),
        question=_text(data.get("pregunta")) or _text(data.get("question")),
        first_comment=_text(data.get("primer_comentario")) or _text(data.get("first_comment")),
        hashtags=tuple(_list(data.get("hashtags"))),
        changes=tuple(_list(data.get("cambios")) if isinstance(data.get("cambios"), list)
                      else [])[:3],
    )


def parse_ollama_reply(content: str) -> dict | None:
    """Single-string view of :func:`parse_draft`, kept for older callers."""
    draft = parse_draft(content)
    if draft is None:
        return None
    return {"caption": draft.caption, "hashtags": list(draft.hashtags)}


# --------------------------------------------------------------------------- #
# Checks and tidy-up
# --------------------------------------------------------------------------- #

# Scripts a Spanish description never needs; CJK punctuation («，») included,
# since stripping the ideographs alone would leave it behind.
_FOREIGN_RE = re.compile(
    "[Ͱ-ϿЀ-ԯ֐-ۿऀ-෿฀-࿿ᄀ-ᇿ"
    "　-〿぀-ヿ㄀-㆏㐀-䶿一-鿿ꥠ-꥿"
    "가-힯豈-﫿＀-￯]")
# English that leaked into Spanish text. Allowed when the video itself says it
# (a book title, «Think Week»).
_ENGLISH_RE = re.compile(
    r"\b(the|you|your|and|with|for|this|that|is|are|of|to|my|it|be|just|what|how|why|when|"
    r"need|needs|don't|can't|i'm|get|make|feel|life|love|self|stress|single|weekend|"
    r"mindset|lifestyle)\b", re.IGNORECASE)
# English hashtags a Spanish account should use in Spanish.
_ENGLISH_TAGS = {
    "solitude": "soledad", "loneliness": "soledad", "habits": "habitos", "weekend": "findesemana",
    "attention": "atencion", "love": "amor", "life": "vida", "selfcare": "autocuidado",
    "mentalhealth": "saludmental", "psychology": "psicologia", "relationships": "relaciones",
    "motivation": "motivacion", "productivity": "productividad", "study": "estudio",
    "reading": "lectura", "happiness": "felicidad", "anxiety": "ansiedad",
    "philosophy": "filosofia", "stoicism": "estoicismo", "neuroscience": "neurociencia",
    "memory": "memoria", "sleep": "sueño",
}
_STOCK_OPENER_RE = re.compile(
    r"^[¿¡\s]*(sab[ií]as que|descubre|secretos? de|no te lo vas a creer|te sorprender[aá]|"
    r"incre[ií]ble|atenci[oó]n|mira esto)", re.IGNORECASE)
_EMOJI_RE = re.compile(
    "(?:[\U0001F000-\U0001FAFF☀-➿⬀-⯿]"
    "(?:️|[\U0001F3FB-\U0001F3FF]|‍[\U0001F000-\U0001FAFF☀-➿])*)")
_INLINE_TAG_RE = re.compile(r"(?<!\w)#[^\s#]+")


def _is_foreign(char: str) -> bool:
    return bool(_FOREIGN_RE.match(char)) or not hashtags_svc.is_latin(char)


def _foreign_chars(text: str) -> str:
    return "".join(dict.fromkeys(c for c in text if _is_foreign(c)))


def _open_marks(text: str) -> str:
    """Add the «¿» and «¡» a question or exclamation is missing."""
    out = []
    for sentence in _SENTENCE_SPLIT.split(text):
        core = _EMOJI_RE.sub("", sentence).rstrip()
        for close, opening in (("?", "¿"), ("!", "¡")):
            if core.endswith(close) and opening not in sentence:
                # A question can start mid-sentence («Cena sola? Haz…» is two).
                lead = re.match(r"^[\W_]*?(?=\w)", sentence)
                at = lead.end() if lead else 0
                sentence = sentence[:at] + opening + sentence[at:]
        out.append(sentence)
    return " ".join(out)


_QUOTE_PAIRS = {"«": "»", "“": "”", '"': '"', "'": "'"}


def _unwrap_quotes(text: str) -> str:
    """«Una frase entre comillas» -> Una frase…; «Walden» dentro de la frase se queda."""
    text = text.strip()
    while len(text) >= 2 and text[0] in _QUOTE_PAIRS and text[-1] == _QUOTE_PAIRS[text[0]]:
        text = text[1:-1].strip()
    return text


def _tidy_line(text: str) -> str:
    text = _INLINE_TAG_RE.sub("", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = _unwrap_quotes(reelinfo.SERIES_COUNTER_RE.sub("", text).strip(" ·"))
    if not text:
        return ""
    text = _open_marks(text)
    return text[:1].upper() + text[1:]


def _shorten(text: str, limit: int) -> str:
    """Cut an overlong line where it reads best: a sentence, a clause, a word.

    Only a safety net: an overlong line first goes back to the model as a
    problem to fix, because a cut at «…suelen caer al…» reads as broken.
    """
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s|$)", cut)]
    if ends and ends[-1] > limit * 0.6:
        return cut[:ends[-1]].strip()
    clause = max(cut.rfind(", "), cut.rfind("; "), cut.rfind(": "))
    if clause > limit * 0.6:
        return cut[:clause].rstrip(" ,;:") + "…"
    boundary = cut.rfind(" ")
    return (cut[:boundary] if boundary > limit * 0.5 else cut).rstrip(" ,;:·") + "…"


def _tidy_tags(tags: Sequence[str]) -> tuple[str, ...]:
    out = []
    for raw in tags:
        tag = hashtags_svc.normalize(str(raw))
        if tag is None:
            continue
        body = tag.lstrip("#")
        tag = "#" + _ENGLISH_TAGS.get(hashtags_svc.fold(body), body)
        if not hashtags_svc.is_generic(tag):
            out.append(tag)
    return tuple(hashtags_svc.normalize_all(out))


def tidy(draft: Draft) -> Draft:
    """The fixes that need no model: marks, quotes, stray tags, one emoji."""
    lines = [_tidy_line(draft.hook), _tidy_line(draft.fact), _tidy_line(draft.question)]
    seen_emoji = False

    def keep_first(match: re.Match) -> str:
        nonlocal seen_emoji
        if seen_emoji:
            return ""
        seen_emoji = True
        return match.group(0)

    lines = [re.sub(r"\s{2,}", " ", _EMOJI_RE.sub(keep_first, line)).strip() for line in lines]
    title = re.sub(r"\s{2,}", " ", _EMOJI_RE.sub("", _tidy_line(draft.title))).strip()
    return replace(draft, title=_shorten(title, TITLE_MAX_CHARS), hook=lines[0], fact=lines[1],
                   question=lines[2], hashtags=_tidy_tags(draft.hashtags))


def _content_words(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(hashtags_svc.fold(text)) if len(w) > 3]


def _same_opening(a: str, b: str) -> bool:
    wa, wb = _content_words(a)[:3], _content_words(b)[:3]
    if len(wa) >= 2 and wa[:2] == wb[:2]:
        return True
    return SequenceMatcher(None, hashtags_svc.fold(a), hashtags_svc.fold(b)).ratio() > 0.75


# Opening words that say nothing about a formula when they repeat.
_NEUTRAL_OPENERS = frozenset("el la los las un una unos unas tu tus si de en lo al a y".split())


def _first_word(line: str) -> str:
    words = _WORD_RE.findall(hashtags_svc.fold(line or ""))
    return words[0] if words and words[0] not in _NEUTRAL_OPENERS else ""


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text and text.strip() else ""


def caption_issues(draft: Draft, *, source: str = "", spoken_hook: str = "",
                   recent: Sequence[str] = ()) -> tuple[list[str], list[str]]:
    """What is wrong with a draft, as ``(hard, soft)`` lists of instructions.

    Hard problems make a draft unusable (another alphabet, English, no hook);
    soft ones are worth one editing pass. Both are phrased as orders, because
    they go back to the model verbatim.
    """
    hard: list[str] = []
    soft: list[str] = []
    text = "\n".join(draft.lines)
    foreign = _foreign_chars(text + " ".join(draft.hashtags))
    if foreign:
        hard.append(f"hay caracteres de otro alfabeto ({foreign[:8]}): escríbelo todo en español")
    allowed = {w.lower() for w in _ENGLISH_RE.findall(source)}
    english = sorted({w.lower() for w in _ENGLISH_RE.findall(text)} - allowed)
    if english:
        hard.append("hay palabras en inglés (" + ", ".join(english) + "): escríbelo todo en español")
    if not draft.hook:
        hard.append("falta el gancho")
        return hard, soft
    if _STOCK_OPENER_RE.search(draft.hook):
        soft.append("el gancho empieza con una fórmula gastada: nombra el tema concreto")
    if len(draft.hook) > HOOK_MAX_CHARS:
        soft.append(f"el gancho tiene {len(draft.hook)} caracteres: déjalo en 100 como mucho")
    if spoken_hook and SequenceMatcher(None, hashtags_svc.fold(draft.hook),
                                       hashtags_svc.fold(spoken_hook)).ratio() > 0.8:
        soft.append("el gancho copia la primera frase del vídeo, que ya se oye: di otra cosa")
    firsts = [_first_line(p) for p in recent if _first_line(p)]
    for first in firsts:
        if _same_opening(draft.hook, first):
            soft.append(f"el gancho arranca igual que otra descripción reciente («{first[:60]}»): "
                        "cambia el arranque")
            break
    else:
        # A series of fifty that all open with «Cómo…» reads like a template,
        # even when no two are alike.
        opener = _first_word(draft.hook)
        if opener and sum(_first_word(f) == opener for f in firsts[:5]) >= 2:
            word = _WORD_RE.findall(draft.hook)[0]
            soft.append(f"las últimas descripciones también empiezan por «{word}»: "
                        "empieza de otra forma (el dato, el autor, la situación, una pregunta)")
    if not draft.fact:
        soft.append("falta el dato concreto del vídeo")
    elif len(draft.fact) > FACT_MAX_CHARS:
        soft.append(f"el dato tiene {len(draft.fact)} caracteres: déjalo en 170 como mucho")
    if not draft.question:
        soft.append("falta la pregunta para comentar")
    elif not draft.question.rstrip().endswith("?"):
        soft.append("la pregunta no es una pregunta")
    if "·" in text:
        soft.append("quita la lista de palabras clave separadas por «·»: escribe frases")
    if len(_tidy_tags(draft.hashtags)) < 3:
        soft.append("faltan hashtags del tema (de 3 a 5, en español)")
    return hard, soft


# --------------------------------------------------------------------------- #
# Ollama: writing
# --------------------------------------------------------------------------- #

def _chat(settings: Settings, model: str, messages: list[dict], schema: dict,
          temperature: float) -> str | None:
    """One chat call that must return JSON. None on any failure."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        # Models with "thinking" (Gemma 4, Qwen 3) would spend the token budget
        # reasoning and hand back an empty JSON, as ContentBuilder found out.
        # Models without it ignore the flag.
        "think": False,
        "format": schema,
        "options": {"temperature": temperature, "top_p": 0.9, "num_predict": 500},
        "keep_alive": settings.ollama_keep_alive,
    }
    url = settings.ollama_url.rstrip("/")
    try:
        with httpx.Client(timeout=settings.ollama_timeout_seconds) as client:
            resp = client.post(f"{url}/api/chat", json=payload)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 - the heuristic path takes over
        log.warning("ollama failed: %s", exc)
        return None
    if not isinstance(body, dict):
        return None
    message = body.get("message")
    return (message.get("content") if isinstance(message, dict) else body.get("response")) or ""


def write_with_ollama(status: OllamaStatus, ctx: Context, *, transcript: Transcript,
                      sheet: ReelInfo | None = None,
                      temperatures: Sequence[float] = (0.6, 0.35)) -> tuple[Draft | None, str]:
    """Draft, check, edit once, keep the best. ``(None, why)`` when unusable.

    The editing pass is ContentBuilder's editor in small: the model gets its own
    answer back with the problems the checks found, phrased as orders, and
    the version with fewer serious problems wins — an edit can make a good
    draft worse, so it never wins by default. ``temperatures`` are the fresh
    starts it may take; the editor asks for one, since someone is waiting.
    """
    settings = ctx.settings
    if sheet is not None:
        system, user, schema = SHEET_SYSTEM, sheet_prompt(sheet, ctx), SHEET_SCHEMA
        source, spoken = sheet.script, sheet.hook
    else:
        system, user, schema = TRANSCRIPT_SYSTEM, transcript_prompt(transcript, ctx), TRANSCRIPT_SCHEMA
        source = transcript.text
        spoken = _SENTENCE_SPLIT.split(transcript.text.strip())[0] if transcript.text else ""
    base = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def check(draft: Draft) -> tuple[list[str], list[str]]:
        return caption_issues(draft, source=source, spoken_hook=spoken,
                              recent=ctx.recent_captions)

    scored: list[tuple[int, int, int, Draft]] = []
    for temperature in temperatures:
        draft = parse_draft(_chat(settings, status.model, base, schema, temperature) or "")
        if draft is None:
            continue
        draft = tidy(draft)
        hard, soft = check(draft)
        scored.append((len(hard), len(soft), len(scored), draft))
        if (hard or soft) and settings.caption_editor:
            fix = ("Revisa tu respuesta: " + "; ".join(hard + soft)
                   + ". Corrígelo sin tocar lo que ya está bien y devuelve el JSON completo.")
            edited = parse_draft(_chat(settings, status.model, base + [
                {"role": "assistant", "content": draft.as_json()},
                {"role": "user", "content": fix},
            ], schema, 0.3) or "")
            if edited is not None:
                edited = tidy(edited)
                e_hard, e_soft = check(edited)
                scored.append((len(e_hard), len(e_soft), len(scored), edited))
        if any(h == 0 for h, *_ in scored):
            break
        # Only a draft that is still unusable earns a fresh, cooler start.
    if not scored:
        return None, f"Ollama ({status.model}) no devolvió un texto válido"
    hard_count, _, _, best = min(scored, key=lambda s: s[:3])
    if hard_count:
        problems, _ = check(best)
        return None, f"Ollama ({status.model}) no dio un texto limpio ({'; '.join(problems)})"
    # Last line of defence: nothing of another alphabet reaches TikTok, and a
    # line still too long after the edit is cut where it reads best.
    return replace(
        best,
        hook=_shorten(_strip_foreign(best.hook), int(HOOK_MAX_CHARS * 1.3)),
        fact=_shorten(_strip_foreign(best.fact), int(FACT_MAX_CHARS * 1.3)),
        question=_shorten(_strip_foreign(best.question), int(QUESTION_MAX_CHARS * 1.3)),
    ), ""


def _strip_foreign(text: str) -> str:
    return re.sub(r"\s{2,}", " ", "".join(c for c in text if not _is_foreign(c))).strip()


# --------------------------------------------------------------------------- #
# Without a language model
# --------------------------------------------------------------------------- #

def clean_caption(text: str, limit: int = CAPTION_MAX_CHARS) -> str:
    """Strip hashtags, quotes and stray whitespace; cut at a word boundary."""
    text = re.sub(r"#\w+", "", text or "")
    text = _unwrap_quotes(text)
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        cut = text[:limit]
        boundary = cut.rfind(" ")
        text = (cut[:boundary] if boundary > limit * 0.5 else cut).rstrip(" ,;:") + "…"
    return text[:1].upper() + text[1:] if text else ""


def heuristic_caption(transcript_text: str, limit: int = CAPTION_MAX_CHARS) -> str:
    """The opening line(s) of the video, tidied up: usually the hook itself."""
    text = _FILLER_TOKENS.sub("", transcript_text or "").strip()
    text = _LEADING_FILLER.sub("", text).strip()
    if not text:
        return ""
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    chosen: list[str] = []
    length = 0
    for sentence in sentences:
        extra = len(sentence) + (1 if chosen else 0)
        if chosen and length + extra > limit:
            break
        chosen.append(sentence)
        length += extra
        if length >= limit * 0.6:
            break
    return clean_caption(" ".join(chosen), limit)


def sheet_fallback(sheet: ReelInfo) -> Draft:
    """A sheet without a model: its TikTok texts (format 2), or else its title
    and its first question to comment on.

    The draft caption line of a format-1 sheet is left out on purpose: it is
    the title followed by keywords glued with «·», which is exactly what the
    model would remove.
    """
    texts = sheet.texts_for("tiktok")
    if texts is not None:
        lines = [line.strip() for line in texts.description.splitlines() if line.strip()]
        question = lines[-1] if len(lines) > 1 and lines[-1].endswith("?") else ""
        body = lines[1:-1] if question else lines[1:]
        return Draft(title=texts.title, hook=lines[0] if lines else texts.title, fact=" ".join(body),
                     question=question, first_comment=texts.first_comment, hashtags=texts.tags)
    hook = sheet.title or sheet.hook
    if not hook and sheet.caption_lines:
        hook = sheet.caption_lines[0].split(" · ")[0]
    return tidy(Draft(title=sheet.title, hook=hook, question=sheet.pins[0] if sheet.pins else "",
                      first_comment="\n".join(sheet.pins) if sheet.pins else "",
                      hashtags=sheet.hashtags))


# --------------------------------------------------------------------------- #
# The whole thing, for one video
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Context:
    settings: Settings
    fixed_hashtags: list[str] = field(default_factory=list)
    bank: hashtags_svc.Bank = hashtags_svc.DEFAULT_BANK
    scores: dict = field(default_factory=dict)
    account_avg_views: float = 0.0
    has_audio: bool = True
    # What else the model is told: the file's name (series and topic, when it
    # is descriptive), the queue, the account's latest descriptions, and the
    # .txt sheet that came with the video.
    file_name: str = ""
    queue_name: str = ""
    recent_captions: tuple[str, ...] = ()
    sheet: ReelInfo | None = None
    # What ffprobe measured: a format-2 sheet says its video's duration, which
    # tells whether it belongs to this video without listening to it.
    duration: float | None = None


@dataclass(frozen=True)
class Composition:
    title: str
    caption: str
    first_comment: str
    hashtags: list[str]
    # whisper | whisper+ollama | ficha | ficha+ollama | fallback
    source: str
    note: str = ""
    transcript: Transcript = field(default_factory=Transcript)
    seconds: float = 0.0
    changes: tuple[str, ...] = ()
    # The sheet's verdict when it asks for a human look, else empty.
    review: str = ""
    # What the hashtags were chosen from, so :func:`retag` can choose them
    # again against another platform's numbers without asking the model.
    niche: tuple[str, ...] = ()
    topic_text: str = ""
    min_topic: int = 3


def compose(transcript: Transcript, ctx: Context, sheet: ReelInfo | None = None,
            notes: Sequence[str] = ()) -> Composition:
    """Turn a transcript (or a sheet) into caption + hashtags, with or without Ollama."""
    settings = ctx.settings
    notes = [n for n in notes if n]
    limit = min(settings.hashtag_max, hashtags_svc.TIKTOK_MAX_TAGS)
    review = ""
    if sheet is not None and sheet.needs_review:
        review = "ContentBuilder pide revisar el guion a mano" + (
            f": {sheet.verdict_notes}" if sheet.verdict_notes else "")
        notes.insert(0, review)

    if sheet is None and not transcript.has_speech:
        tags = hashtags_svc.select_tags(
            fixed=ctx.fixed_hashtags, niche=[], transcript="", bank=ctx.bank,
            scores=ctx.scores, account_avg=ctx.account_avg_views, limit=limit,
            reach=settings.hashtag_reach, min_topic=0,
        )
        notes.append("el vídeo no tiene pista de audio" if not ctx.has_audio
                     else "no se detectó voz: sólo hashtags")
        return Composition(topic_from_filename(ctx.file_name)[:TITLE_MAX_CHARS], "", "", tags,
                           "fallback", "; ".join(notes), transcript, min_topic=0)

    status = probe_ollama(settings)
    draft: Draft | None = None
    source = "ficha" if sheet is not None else "whisper"
    if status.reachable:
        draft, why = write_with_ollama(status, ctx, transcript=transcript, sheet=sheet)
        if draft is not None:
            source += "+ollama"
        else:
            notes.append(why + ("; se usó la ficha tal cual" if sheet is not None
                                else "; se usó la primera frase"))
    elif settings.ollama_url:
        notes.append(status.error)
    if draft is None:
        draft = sheet_fallback(sheet) if sheet is not None else Draft(
            title=topic_from_filename(ctx.file_name) or "",
            hook=heuristic_caption(transcript.text),
            hashtags=tuple(hashtags_svc.keyword_tags(transcript.text, ctx.bank)))
    topic_text = sheet.script if sheet is not None else transcript.text
    tags = hashtags_svc.select_tags(
        fixed=ctx.fixed_hashtags, niche=list(draft.hashtags), transcript=topic_text,
        bank=ctx.bank, scores=ctx.scores, account_avg=ctx.account_avg_views, limit=limit,
        reach=settings.hashtag_reach,
    )
    title = draft.title or _shorten(_tidy_line(draft.hook), TITLE_MAX_CHARS)
    return Composition(title, draft.caption, draft.first_comment, tags, source, "; ".join(notes),
                       transcript, changes=draft.changes, review=review,
                       niche=tuple(draft.hashtags), topic_text=topic_text)


def retag(composition: Composition, ctx: Context, scores: dict,
          account_avg: float = 0.0) -> list[str]:
    """The hashtags of ``composition`` chosen again with other numbers.

    The library writes one text per video and hashtags per platform: what
    worked on TikTok is not what worked on YouTube. Same rules and inputs as
    :func:`compose`, so with no numbers on either side the lists are equal.
    """
    settings = ctx.settings
    return hashtags_svc.select_tags(
        fixed=ctx.fixed_hashtags, niche=list(composition.niche),
        transcript=composition.topic_text, bank=ctx.bank, scores=scores,
        account_avg=account_avg, limit=min(settings.hashtag_max, hashtags_svc.TIKTOK_MAX_TAGS),
        reach=settings.hashtag_reach, min_topic=composition.min_topic,
    )


def check_sheet(path: Path, sheet: ReelInfo, ctx: Context) -> tuple[bool, Transcript, str]:
    """Does the sheet belong to this video? ``(keep, what was heard, note)``.

    The name already matches (that is how the sheet was found); the audio
    settles it. Whisper listens to the first ``CAPTION_SHEET_CHECK_SECONDS``
    and the words must follow the script; when the opening has too little
    speech to tell, it listens to the whole video. A rejected sheet comes back
    with the whole video's transcript, ready for the path without a sheet.
    """
    settings = ctx.settings
    if not ctx.has_audio:
        return True, Transcript(), "ficha .txt sin comprobar: el vídeo no tiene audio"
    if settings.caption_sheet_check_seconds <= 0:
        return True, Transcript(), ""
    heard = transcribe(path, settings, max_seconds=settings.caption_sheet_check_seconds)
    complete = False
    score = reelinfo.script_match(sheet.script, heard.text)
    if score is None:
        heard, complete = transcribe(path, settings), True
        score = reelinfo.script_match(sheet.script, heard.text)
    if score is None:
        return True, heard, "ficha .txt sin comprobar: apenas hay voz"
    if score < SHEET_MATCH_MIN:
        if not complete:
            heard = transcribe(path, settings)
        return False, heard, (f"el .txt no parece de este vídeo ({score:.0%} de coincidencia "
                              "con el audio): se ignoró")
    return True, heard, f"ficha .txt comprobada con el audio ({score:.0%})"


def generate(path: Path, ctx: Context) -> Composition:
    """Check the sheet (if any), transcribe if needed, compose. Blocking.

    A format-2 sheet is checked by its duration (free); a format-1 sheet, or
    one without a duration, by listening to the first seconds.
    """
    started = time.monotonic()
    sheet, notes = ctx.sheet, []
    match, note = reelinfo.duration_match(sheet, ctx.duration) if sheet is not None else (None, "")
    if match is True:
        # Same duration as the sheet says: no need to listen.
        notes.append(note)
        transcript = Transcript(text=sheet.script, language="es", duration=ctx.duration or 0.0)
    elif match is False:
        notes.append(f"{note}: se ignoró")
        sheet = None
        transcript = transcribe(path, ctx.settings) if ctx.has_audio else Transcript()
    elif sheet is not None:
        keep, heard, note = check_sheet(path, sheet, ctx)
        notes.append(note)
        if keep:
            # The script is the transcript, without a single misheard word.
            transcript = Transcript(text=sheet.script, language=heard.language or "es",
                                    duration=heard.duration)
        else:
            sheet, transcript = None, heard
    else:
        transcript = transcribe(path, ctx.settings) if ctx.has_audio else Transcript()
    result = compose(transcript, ctx, sheet, notes)
    return replace(result, seconds=round(time.monotonic() - started, 1))
