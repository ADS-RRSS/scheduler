"""Automatic texts and hashtags.

Whisper is never run here: ``fake_whisper`` swaps the transcriber for one that
returns a canned transcript, and Ollama is the fake one in tools/mock_zernio.py.
Everything downstream — the caption, the hashtag choice, the queue states, the
publisher waiting for the text — runs for real.
"""
from __future__ import annotations

import pytest

from app.services import autocaption, hashtags
from tests.helpers import (
    MOCK_BASE,
    configure_mock,
    drain,
    first_video,
    make_account,
    mock_state,
    put_schedule,
    reset_mock,
    tick,
    upload_sample,
)

TRANSCRIPT = (
    "Hola a todos. Hoy os cuento los cinco hábitos de alto rendimiento que cambiaron mi "
    "productividad. El primero: madrugar, aunque cueste. La disciplina no es motivación, "
    "es rutina."
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture
def ai_on():
    """Turn the feature on for one test (settings are a cached singleton)."""
    from app.config import get_settings

    s = get_settings()
    saved = (s.caption_ai_enabled, s.ollama_url, s.ollama_model, s.caption_max_wait_minutes)
    s.caption_ai_enabled, s.ollama_url, s.ollama_model = True, "", ""
    autocaption.reset_probe_cache()
    yield s
    s.caption_ai_enabled, s.ollama_url, s.ollama_model, s.caption_max_wait_minutes = saved
    autocaption.reset_probe_cache()


@pytest.fixture
def fake_whisper(monkeypatch):
    """Replace Whisper with a transcript of our choosing (or a failure).

    Every call is recorded with its ``max_seconds``: the sheet check listens
    to the opening only, and the tests check it does.
    """
    calls: list[float | None] = []

    def install(text: str = TRANSCRIPT, language: str = "es", error: Exception | None = None):
        def transcribe(path, settings, *, max_seconds=None):
            calls.append(max_seconds)
            if error is not None:
                raise error
            return autocaption.Transcript(text=text, language=language,
                                          duration=float(max_seconds or 24.0))
        monkeypatch.setattr(autocaption, "transcribe", transcribe)
        return calls
    return install


async def run_captioner() -> bool:
    from app.scheduler.captioner import captioner
    return await captioner.run_once()


async def queue_one(client, sample_video, caption: str = "") -> tuple[int, dict]:
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    video = await upload_sample(client, account_id, sample_video, caption=caption)
    return account_id, video


# --------------------------------------------------------------------------- #
# Hashtags: pure logic
# --------------------------------------------------------------------------- #

def test_hashtags_are_normalised_and_deduped_by_accent_folding():
    assert hashtags.normalize("#Hábitos!") == "#hábitos"
    assert hashtags.normalize("  ES PAÑA ") == "#españa"
    assert hashtags.normalize("12") is None and hashtags.normalize("#") is None
    assert hashtags.normalize("x" * 41) is None
    assert hashtags.normalize_all(["hábitos", "#habitos", "HABITOS", "#fyp"]) == ["#hábitos", "#fyp"]
    assert hashtags.extract("Texto #uno y #Dos. #uno otra vez") == ["#uno", "#dos"]


def test_fixed_tags_come_first_and_survive_the_cap():
    bank = hashtags.load_bank("data/hashtags.json")
    tags = hashtags.select_tags(
        fixed=["#miMarca", "otramarca"], niche=["habitos", "productividad", "parati"],
        transcript=TRANSCRIPT, bank=bank, limit=4,
    )
    assert tags[:2] == ["#mimarca", "#otramarca"]
    assert len(tags) == 4
    # "parati" offered as a niche tag is a reach tag in disguise: not counted as niche.
    assert "#habitos" in tags and "#productividad" in tags


def test_topic_tags_come_from_a_clear_bank_category_and_reach_tags_only_on_request():
    bank = hashtags.load_bank("data/hashtags.json")
    # Seven habit keywords: clearly about habits. One «motivación»: not about motivation.
    assert [name for name, _ in hashtags.detect_categories(TRANSCRIPT, bank)] == [
        "habitos_productividad"]
    tags = hashtags.select_tags(fixed=[], niche=[], transcript=TRANSCRIPT, bank=bank)
    assert tags == ["#habitos", "#productividad"]
    # #parati/#fyp say nothing about the video: they only come when asked for.
    with_reach = hashtags.select_tags(fixed=[], niche=[], transcript=TRANSCRIPT, bank=bank, reach=2)
    assert with_reach == ["#habitos", "#productividad", "#parati", "#fyp"]


def test_a_word_said_in_passing_does_not_make_a_category():
    bank = hashtags.load_bank("data/hashtags.json")
    lonely = ("Suena el microondas y tu pulgar ya desliza el móvil. Esos huecos a solas "
              "servían para respirar; mira el plato girar y deja que pase el minuto.")
    # «móvil» once used to make it #tecnologia, «plato» #receta.
    assert hashtags.detect_categories(lonely, bank) == []
    assert hashtags.detect_categories(lonely, bank, strict=False)
    # «unas» is an article; «uñas» (nails) is the beauty keyword.
    assert hashtags.fold("uñas", keep_enye=True) == "uñas" and hashtags.fold("uñas") == "unas"
    words = "unas semanas después unas cuantas veces unas horas"
    assert all(name != "belleza_moda"
               for name, _ in hashtags.detect_categories(words, bank, strict=False))


def test_tags_in_another_alphabet_are_dropped_whole():
    assert hashtags.normalize("#tiempo独自") is None
    assert hashtags.normalize("#сон") is None
    assert hashtags.normalize("#España") == "#españa"
    assert hashtags.normalize("#1ºdebachillerato") == "#1ºdebachillerato"


def test_tiktok_counts_five_tags_and_generic_ones_never_pass_as_topic():
    bank = hashtags.load_bank("data/hashtags.json")
    tags = hashtags.select_tags(
        fixed=["#marca"], niche=["uno", "dos", "tres", "cuatro", "cinco", "fyp", "viral"],
        transcript="", bank=bank,
    )
    assert len(tags) == hashtags.TIKTOK_MAX_TAGS == 5
    assert tags[0] == "#marca" and "#fyp" not in tags and "#viral" not in tags


def test_evidence_promotes_proven_tags_and_demotes_weak_ones():
    scores = {
        "#viral": hashtags.TagScore(uses=5, views_avg=100.0),    # weak: far under average
        "#parati": hashtags.TagScore(uses=4, views_avg=9000.0),  # proven
        "#fyp": hashtags.TagScore(uses=1, views_avg=99999.0),    # one lucky video: unknown
    }
    ranked = hashtags.rank_by_evidence(["#fyp", "#viral", "#parati"], scores, account_avg=1000.0)
    assert ranked == ["#parati", "#fyp", "#viral"]


def test_keyword_tags_skip_filler_and_prefer_topic_words():
    bank = hashtags.load_bank("data/hashtags.json")
    tags = hashtags.keyword_tags(TRANSCRIPT, bank)
    assert "#cuento" not in tags and "#cinco" not in tags and "#hola" not in tags
    assert set(tags) <= {"#productividad", "#rendimiento", "#disciplina", "#hábitos",
                         "#motivación", "#madrugar", "#rutina", "#cambiaron"}
    assert "#productividad" in tags


# --------------------------------------------------------------------------- #
# Captions: pure logic
# --------------------------------------------------------------------------- #

def test_heuristic_caption_is_the_hook_without_the_greeting():
    caption = autocaption.heuristic_caption(TRANSCRIPT)
    assert caption.startswith("Hoy os cuento los cinco hábitos")
    assert "Hola a todos" not in caption
    assert len(caption) <= autocaption.CAPTION_MAX_CHARS


def test_heuristic_caption_cuts_long_sentences_at_a_word():
    caption = autocaption.heuristic_caption("palabra " * 60)
    assert caption.endswith("…") and len(caption) <= autocaption.CAPTION_MAX_CHARS + 1
    assert not caption.endswith("palabr…")


def test_ollama_reply_parsing_is_forgiving():
    parse = autocaption.parse_ollama_reply
    assert parse('{"caption": "Hola", "hashtags": ["a", "b"]}') == {"caption": "Hola", "hashtags": ["a", "b"]}
    fenced = '```json\n{"caption": "Gancho", "hashtags": "uno, dos #tres"}\n```'
    assert parse(fenced) == {"caption": "Gancho", "hashtags": ["uno", "dos", "tres"]}
    assert parse("Claro, aquí tienes un texto.") is None
    assert parse('{"caption": ""}') is None and parse("") is None
    draft = autocaption.parse_draft(
        '{"gancho": "Uno", "dato": "Dos.", "pregunta": "¿Tres?", "hashtags": ["a"], '
        '"cambios": ["x", "y", "z", "w"]}')
    assert draft.caption == "Uno\nDos.\n¿Tres?" and draft.changes == ("x", "y", "z")


def test_no_speech_means_hashtags_only():
    from app.config import get_settings
    ctx = autocaption.Context(settings=get_settings(), fixed_hashtags=["#marca"],
                              bank=hashtags.load_bank("data/hashtags.json"))
    result = autocaption.compose(autocaption.Transcript(text="eh"), ctx)
    assert result.caption == "" and result.source == "fallback"
    assert result.hashtags == ["#marca"], "sin voz no hay tema: sólo los fijos"
    assert "voz" in result.note


# --------------------------------------------------------------------------- #
# Checks and tidy-up: what a small model got wrong, measured on real texts
# --------------------------------------------------------------------------- #

def test_tidy_fixes_what_needs_no_model():
    draft = autocaption.tidy(autocaption.Draft(
        hook="Cena sola? Haz como si fueras un invitado 🍽️ 😂 #cena",
        fact="«Resuelves problemas en la ducha? 🤔» · 4/50",
        question="Qué haces tú",
        hashtags=("#tiempo独自", "solitude", "#fyp", "#Psicología", "receta"),
    ))
    assert draft.hook == "¿Cena sola? Haz como si fueras un invitado 🍽️"
    assert draft.fact == "¿Resuelves problemas en la ducha?", "un emoji en total, sin contador"
    assert draft.hashtags == ("#soledad", "#psicología", "#receta")


def test_checks_catch_other_alphabets_english_and_stock_openers():
    issues = autocaption.caption_issues
    hard, _ = issues(autocaption.Draft(hook="Ver las estrellas después de un error社交失误，宇宙"))
    assert hard and "alfabeto" in hard[0]
    hard, _ = issues(autocaption.Draft(hook="Bill Gates, una semana al año de reflexión. ¿Lo needs?"))
    assert hard and "inglés" in hard[0] and "needs" in hard[0]
    # English the video itself says (a title, a name) is not a leak.
    hard, _ = issues(autocaption.Draft(hook="La Think Week de Gates: stress cero"),
                     source="Bill Gates lo llamaba reducir el stress con su think week.")
    assert not hard
    assert not issues(autocaption.Draft(hook="¿Te hablas en 1º o 3º persona?"))[0]
    _, soft = issues(autocaption.Draft(
        hook="¿Sabías que el ruido te agota?", fact="", question="Cuéntamelo",
        hashtags=("#ruido",)), recent=["¿Sabías que el silencio cura?"])
    assert any("fórmula gastada" in s for s in soft)
    assert any("falta el dato" in s for s in soft)
    assert any("no es una pregunta" in s for s in soft)
    assert any("faltan hashtags" in s for s in soft)


def test_a_draft_with_problems_gets_one_editing_pass(monkeypatch):
    from app.config import get_settings
    replies = [
        '{"gancho": "Marco Aurelio y el error社交失误", "dato": "Mira las estrellas.", '
        '"pregunta": "¿Y tú?", "hashtags": ["estoicismo"]}',
        '{"gancho": "Marco Aurelio y el error social", "dato": "Mirar las estrellas relativiza '
        'un error pequeño.", "pregunta": "¿Qué error te pesa hoy?", '
        '"hashtags": ["estoicismo", "marcoaurelio", "filosofia"]}',
    ]
    sent = []

    def chat(settings, model, messages, schema, temperature):
        sent.append(messages)
        return replies.pop(0)

    monkeypatch.setattr(autocaption, "_chat", chat)
    ctx = autocaption.Context(settings=get_settings(), bank=hashtags.load_bank("data/hashtags.json"))
    status = autocaption.OllamaStatus(True, model="m")
    draft, why = autocaption.write_with_ollama(
        status, ctx, transcript=autocaption.Transcript(text="Marco Aurelio miraba las estrellas."))
    assert why == "" and draft.hook == "Marco Aurelio y el error social"
    assert draft.hashtags == ("#estoicismo", "#marcoaurelio", "#filosofia")
    # The second call carries the first answer and the problems, as orders.
    assert len(sent) == 2 and sent[1][-2]["role"] == "assistant"
    assert "alfabeto" in sent[1][-1]["content"] and "hashtags" in sent[1][-1]["content"]


def test_a_model_that_keeps_slipping_is_not_published(monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(autocaption, "_chat", lambda *a, **k: (
        '{"gancho": "Vivir solo sin estar孤僻", "dato": "", "pregunta": "", "hashtags": []}'))
    ctx = autocaption.Context(settings=get_settings(), bank=hashtags.load_bank("data/hashtags.json"))
    draft, why = autocaption.write_with_ollama(
        autocaption.OllamaStatus(True, model="m"), ctx,
        transcript=autocaption.Transcript(text="Vivir solo no es estar aislado."))
    assert draft is None and "alfabeto" in why


def test_the_default_model_is_a_gemma_and_never_an_embedding_model():
    pick = autocaption.pick_model
    models = [
        {"name": "embeddinggemma:latest", "size": 621_000_000, "details": {"family": "gemma3"}},
        {"name": "qwen2.5:7b-instruct", "size": 4_700_000_000, "details": {"family": "qwen2"}},
        {"name": "gemma4:26b", "size": 18_600_000_000, "details": {"family": "gemma4"}},
        {"name": "gemma3:1b", "size": 815_000_000, "details": {"family": "gemma3"}},
    ]
    assert pick(models) == "gemma4:26b"
    assert pick(models[:2] + models[3:]) == "qwen2.5:7b-instruct", "gemma3:1b es demasiado pequeño"
    assert pick(models[:1]) is None


def test_the_file_name_says_the_series_and_the_topic():
    topic = autocaption.topic_from_filename
    assert topic("reel_094_el-arte-de-estar-solo_vive-oculto-segun-epicuro.mp4") == (
        "el arte de estar solo · vive oculto segun epicuro")
    assert topic("reel_001.mp4") == "" and topic("IMG_2034.MOV") == ""


def test_whisper_hallucinations_are_dropped():
    assert autocaption._is_hallucination("Subtítulos realizados por la comunidad de Amara.org")
    assert not autocaption._is_hallucination("Hoy os cuento cinco hábitos")


# --------------------------------------------------------------------------- #
# The queue
# --------------------------------------------------------------------------- #

async def library_row(client) -> dict:
    """The only video of the library, as the Biblioteca page reads it."""
    (item,) = (await client.get("/api/library/items", params={"q": "sample"})).json()["items"]
    return item


async def test_a_video_without_text_waits_for_one_and_a_written_text_is_final(
        client, sample_video, ai_on):
    await reset_mock()
    account_id = await make_account(client)
    await put_schedule(client, account_id)

    auto = await upload_sample(client, account_id, sample_video, caption="")
    # The library writes it; the queued video waits for the library.
    assert auto["caption_status"] == "pending" and auto["caption_source"] == "biblioteca"
    assert (await library_row(client))["caption_status"] == "pending"

    edited = await client.patch(f"/api/videos/{auto['id']}", json={"caption": "Mi texto"})
    assert edited.status_code == 200
    body = edited.json()
    assert body["caption_status"] == "none" and body["caption_source"] == "manual"
    assert (await client.get("/api/state")).json()["captions_pending"] == 0
    # The library still writes its own, for the other queues; this one keeps the human's.
    assert await run_captioner() is True
    assert (await first_video(client))["caption"] == "Mi texto"
    assert await run_captioner() is False


async def test_the_captioner_writes_text_and_hashtags_from_the_transcript(
        client, sample_video, ai_on, fake_whisper):
    await reset_mock()
    fake_whisper()
    await client.put("/api/settings", json={"hashtags": "#miMarca", "webhook_url": ""})
    _, video = await queue_one(client, sample_video)
    assert (await client.get("/api/state")).json()["library"]["writing"] == 1

    assert await run_captioner() is True
    row = await library_row(client)
    assert row["caption_status"] == "done" and row["caption_source"] == "whisper"
    assert row["transcript"] == TRANSCRIPT
    for platform in ("tiktok", "youtube"):
        texts = row["texts"][platform]
        assert texts["description"].startswith("Hoy os cuento los cinco hábitos")
        assert texts["tags"][0] == "#mimarca"
        assert "#productividad" in texts["tags"] or "#habitos" in texts["tags"]
        assert len(texts["tags"]) <= 6
        assert texts["title"]
    item = await first_video(client)
    assert item["caption_status"] == "done" and item["caption_source"] == "biblioteca"
    assert item["caption"] == row["texts"]["tiktok"]["description"]
    assert item["auto_hashtags"] == row["texts"]["tiktok"]["tags"]
    assert item["caption_generated_at"]
    assert (await client.get("/api/state")).json()["library"]["writing"] == 0

    events = (await client.get("/api/events?kind=library_caption_generated")).json()["events"]
    assert events and "Whisper" in events[0]["message"]


async def test_ollama_writes_the_caption_when_it_is_there(client, sample_video, ai_on, fake_whisper):
    await reset_mock()
    fake_whisper()
    ai_on.ollama_url = MOCK_BASE
    autocaption.reset_probe_cache()
    await queue_one(client, sample_video)

    await run_captioner()
    row = await library_row(client)
    assert row["caption_source"] == "whisper+ollama"
    texts = row["texts"]["tiktok"]
    assert texts["description"] == (
        "Cinco hábitos que sí cambian el día 🔥\n"
        "Madrugar, la disciplina como rutina y tres cambios que se notan en una semana.\n"
        "¿Cuál de los cinco te cuesta más?")
    assert texts["tags"][:3] == ["#habitos", "#productividad", "#rutina"]
    assert row["texts"]["youtube"]["title"] == "Cinco hábitos para rendir más cada día"
    assert row["caption_error"] is None
    assert (await first_video(client))["caption"] == texts["description"]

    (call,) = (await mock_state())["ollama_calls"]
    # No Gemma installed: the smallest chat model of 4 GB or more. The JSON shape is a schema,
    # thinking is off (Gemma 4 returns an empty JSON otherwise), and the
    # transcript is in the prompt.
    assert call["model"] == "mock-big:latest"
    assert call["format"]["required"] == ["titulo", "gancho", "dato", "pregunta", "hashtags"]
    assert call["think"] is False
    assert "cinco hábitos" in call["messages"][-1]["content"]


async def test_a_broken_ollama_falls_back_to_the_opening_line(client, sample_video, ai_on, fake_whisper):
    await reset_mock()
    await configure_mock(ollama_broken=True)
    fake_whisper()
    ai_on.ollama_url = MOCK_BASE
    autocaption.reset_probe_cache()
    await queue_one(client, sample_video)

    await run_captioner()
    row = await library_row(client)
    assert row["caption_status"] == "done" and row["caption_source"] == "whisper"
    assert row["texts"]["tiktok"]["description"].startswith("Hoy os cuento")
    assert "Ollama" in row["caption_error"]
    # The note reaches the queued video too, so the queue can show it.
    assert "Ollama" in (await first_video(client))["caption_error"]


async def test_a_video_without_speech_gets_hashtags_only(client, sample_video, ai_on, fake_whisper):
    await reset_mock()
    fake_whisper(text="")
    await queue_one(client, sample_video)
    await run_captioner()
    row = await library_row(client)
    assert row["caption_status"] == "done" and row["caption_source"] == "fallback"
    assert row["texts"]["tiktok"]["description"] == "" and row["texts"]["tiktok"]["tags"] == []
    assert "voz" in row["caption_error"]
    item = await first_video(client)
    assert item["caption"] == "" and item["auto_hashtags"] == []


async def test_a_whisper_failure_is_shown_on_the_video(client, sample_video, ai_on, fake_whisper):
    await reset_mock()
    fake_whisper(error=RuntimeError("no se pudo abrir el audio"))
    await queue_one(client, sample_video)
    await run_captioner()
    assert (await library_row(client))["caption_status"] == "failed"
    item = await first_video(client)
    assert item["caption_status"] == "failed"
    assert "no se pudo abrir el audio" in item["caption_error"]
    events = (await client.get("/api/events?kind=library_caption_failed")).json()["events"]
    assert events


# --------------------------------------------------------------------------- #
# Publishing around the text
# --------------------------------------------------------------------------- #

async def test_publishing_waits_for_the_text_and_then_uses_it(client, sample_video, ai_on, fake_whisper):
    await reset_mock()
    fake_whisper()
    await client.put("/api/settings", json={"hashtags": "#miMarca", "webhook_url": ""})
    account_id, video = await queue_one(client, sample_video)

    res = await client.post(f"/api/videos/{video['id']}/publish-now")
    assert "texto" in res.json()["message"]
    await tick()
    item = await first_video(client)
    assert item["status"] == "SCHEDULED", "no debe subir hasta tener el texto"
    assert (await mock_state())["posts"] == {}

    await run_captioner()
    await drain(account_id)
    item = await first_video(client)
    assert item["status"] == "PUBLISHED", item["last_error"]

    (post,) = (await mock_state())["posts"].values()
    assert post["content"].startswith("Hoy os cuento los cinco hábitos")
    tags = [t for t in post["content"].split() if t.startswith("#")]
    assert tags[0] == "#mimarca" and tags == item["auto_hashtags"]
    assert item["rendered_caption"] == post["content"]


async def test_publishing_stops_waiting_after_the_limit(client, sample_video, ai_on):
    """Whisper never answers here: the video must still go out."""
    await reset_mock()
    ai_on.caption_max_wait_minutes = 0
    account_id, video = await queue_one(client, sample_video)
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)
    item = await first_video(client)
    assert item["status"] == "PUBLISHED", item["last_error"]
    assert item["caption_status"] == "failed" and "no estuvo listo" in item["caption_error"]
    events = (await client.get("/api/events?kind=caption_timeout")).json()["events"]
    assert events


async def test_regenerate_and_hashtag_edits(client, sample_video, ai_on, fake_whisper):
    await reset_mock()
    fake_whisper()
    _, video = await queue_one(client, sample_video)
    await run_captioner()

    again = await client.post(f"/api/videos/{video['id']}/caption/regenerate")
    assert again.status_code == 200 and again.json()["caption_status"] == "pending"
    await run_captioner()
    assert (await first_video(client))["caption_status"] == "done"

    patched = await client.patch(f"/api/videos/{video['id']}",
                                 json={"hashtags": ["#Hábitos", "habitos", "12", "rutina diaria"]})
    assert patched.json()["auto_hashtags"] == ["#hábitos", "#rutinadiaria"]


async def test_regenerate_is_refused_when_the_feature_is_off(client, sample_video):
    await reset_mock()
    _, video = await queue_one(client, sample_video)
    assert video["caption_status"] == "none"
    resp = await client.post(f"/api/videos/{video['id']}/caption/regenerate")
    assert resp.status_code == 400 and "CAPTION_AI_ENABLED" in resp.json()["detail"]


async def test_health_reports_the_engines(client, ai_on):
    ai_on.ollama_url = MOCK_BASE
    autocaption.reset_probe_cache()
    ai = (await client.get("/api/health")).json()["ai"]
    assert ai["enabled"] is True
    assert ai["whisper"]["available"] is True and ai["whisper"]["model"] == "small"
    assert ai["ollama"]["reachable"] is True and ai["ollama"]["model"] == "mock-big:latest"


async def test_analytics_rank_the_hashtags_that_went_out(client, sample_video):
    await reset_mock()
    await configure_mock(analytics=[{"views": 5000, "likes": 10}])
    await client.put("/api/settings", json={"hashtags": "#uno #dos", "webhook_url": ""})
    account_id, video = await queue_one(client, sample_video, caption="Texto")
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)
    await client.post("/api/analytics/refresh")

    by_tag = (await client.get("/api/analytics")).json()["by_hashtag"]
    assert {t["tag"] for t in by_tag} == {"#uno", "#dos"}
    assert all(t["views_avg"] == 5000 and t["posts"] == 1 and t["trusted"] is False for t in by_tag)
