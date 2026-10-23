"""ContentBuilder's sheet format 2: final texts per platform, published as they come.

The sheet below has the shape ContentBuilder writes since October 2026 (see
app/services/reelinfo.py): its TikTok and YouTube texts are final, so with the
library's switch «Reescribir los textos con IA» off (the default) a video goes
out with them, without Whisper and without Ollama. The sample video lasts 4 s,
and so does the sheet.
"""
from __future__ import annotations

from app.services import autocaption, reelinfo
from tests.helpers import (
    MOCK_BASE,
    default_queue_id,
    enqueue,
    make_account,
    mock_state,
    put_schedule,
    reset_mock,
)
from tests.test_autocaption import ai_on, fake_whisper, run_captioner  # noqa: F401 - fixtures

SHEET = """ContentBuilder · ficha de publicación · v2

· vídeo: reel_01_curiosidades-de-los-gatos_la-leche.mp4
· duración: 4,0 s
· portada: El mito del platito de leche
· serie: curiosidades de los gatos · 1/50
· palabra clave: leche para gatos
· textos: plan externo
· verificación: publicable

— tiktok —
· título: ¿Se le puede dar leche a un gato?
· descripción:
Leche para gatos: el plato clásico que a la mayoría les sienta mal
De adultos dejan de fabricar lactasa, la enzima que digiere la lactosa.
¿Tu gato se vuelve loco con la leche?
· hashtags: #gatos #datoscuriosos #lechedevaca #lactosa #michis
· comentario fijado: ¿Leche sin lactosa o agua fresca: qué le das tú a tu gato cuando pide?

— youtube —
· título: ¿La leche es mala para los gatos? La respuesta está en la lactasa
· descripción:
La leche para gatos es un clásico de dibujos animados, pero a la mayoría les sienta mal.
Al crecer dejan de producir lactasa y la leche de vaca les provoca gases y diarrea.
¿Le has dado leche alguna vez a tu gato?
· hashtags: #gatos #curiosidadesdegatos #lactosa
· comentario fijado: ¿Qué otro mito sobre gatos quieres que expliquemos en el próximo vídeo?

— guion —
El platito de leche parece un regalo. Para tu gato suele ser un problema.

— control —
· gancho: El platito de leche parece un regalo. Para tu gato suele ser un problema.
· idea: La leche no les sienta bien
"""
# A long video: YouTube only, a description with blank lines and chapters, and
# a script whose dialogue dashes look like a section header.
LONG = """ContentBuilder · ficha de publicación · v2

· duración: 10:23
· portada: EL TÚNEL DE BERLÍN
· verificación: ⚠ revisar a mano — 2 nota(s) del verificador

— youtube —
· título: El túnel 57 bajo el Muro de Berlín y el estudiante que lo cavó
· descripción:
El túnel 57 fue una de las fugas más famosas del Muro de Berlín.

Capítulos:
00:00 Introducción
02:10 El túnel
· hashtags: #murodeberlin #historia
· etiquetas: túnel 57, muro de berlín, fugas de la guerra fría
· comentario fijado: ¿Qué otra fuga del Muro de Berlín te gustaría ver contada?

— miniatura —
· texto: BAJO EL MURO

— guion —
[00:00] Introducción
— Nadie sale de aquí —
dijo el guardia.

— control —
· idea: El túnel 57
"""


# --------------------------------------------------------------------------- #
# Reading it
# --------------------------------------------------------------------------- #

def test_format_2_is_read_platform_by_platform():
    info = reelinfo.parse(SHEET)
    assert info.version == 2 and info.has_texts and reelinfo.is_sheet(SHEET)
    assert info.duration == 4.0 and info.video.endswith("la-leche.mp4")
    assert info.title == "El mito del platito de leche" and info.series == "curiosidades de los gatos"
    assert info.keyword == "leche para gatos" and info.origin == "plan externo"
    assert info.verdict == "publicable" and not info.needs_review
    assert info.script.startswith("El platito de leche") and info.idea == "La leche no les sienta bien"
    tiktok, youtube = info.texts_for("tiktok"), info.texts_for("youtube")
    assert tiktok.title == "¿Se le puede dar leche a un gato?"
    assert tiktok.description.splitlines()[-1] == "¿Tu gato se vuelve loco con la leche?"
    assert len(tiktok.description.splitlines()) == 3
    assert tiktok.tags == ("#gatos", "#datoscuriosos", "#lechedevaca", "#lactosa", "#michis")
    assert youtube.title.startswith("¿La leche es mala") and youtube.tags[1] == "#curiosidadesdegatos"
    assert youtube.first_comment.startswith("¿Qué otro mito")
    # What format 1 called the draft caption and the pins, for the AI path.
    assert info.caption_lines[0].startswith("Leche para gatos") and len(info.pins) == 2


def test_a_sheet_signed_with_the_old_project_name_is_still_format_2():
    """Sheets written before the rename sign as PhrasesReels; the library is full of them."""
    old = SHEET.replace("ContentBuilder", "PhrasesReels", 1)
    assert reelinfo.version_of(old) == 2 and reelinfo.is_sheet(old)
    assert reelinfo.parse(old).texts_for("tiktok") == reelinfo.parse(SHEET).texts_for("tiktok")


def test_a_long_video_lends_youtube_to_tiktok_and_keeps_its_dialogue():
    info = reelinfo.parse(LONG)
    assert info.duration == 623.0 and info.needs_review
    youtube = info.texts_for("youtube")
    assert youtube.description.splitlines()[1] == "", "las líneas en blanco de la descripción se quedan"
    assert youtube.description.endswith("02:10 El túnel")
    assert youtube.keywords == ("túnel 57", "muro de berlín", "fugas de la guerra fría")
    assert info.texts_for("tiktok").title == youtube.title, "sin TikTok, la de YouTube"
    assert "Nadie sale de aquí" in info.script and info.idea == "El túnel 57"


def test_the_duration_tells_whether_the_sheet_is_this_videos():
    info = reelinfo.parse(SHEET)
    assert reelinfo.duration_match(info, 4.04)[0] is True
    match, note = reelinfo.duration_match(info, 31.0)
    assert match is False and "31,0 s" in note and "4,0 s" in note
    assert reelinfo.duration_match(info, None)[0] is None
    assert reelinfo.parse_duration("46,8 s") == 46.8 and reelinfo.parse_duration("1:02:03") == 3723


def test_without_a_model_the_draft_is_the_tiktok_text():
    draft = autocaption.sheet_fallback(reelinfo.parse(SHEET))
    assert draft.hook.startswith("Leche para gatos") and draft.question.endswith("leche?")
    assert draft.hashtags[0] == "#gatos" and draft.first_comment.startswith("¿Leche sin lactosa")


# --------------------------------------------------------------------------- #
# Into the library, with the switch off (the default)
# --------------------------------------------------------------------------- #

async def _upload(client, video, text: str, *, name: str = "reel_01", ai: str | None = None) -> dict:
    files = [("files", (f"{name}.mp4", video.read_bytes(), "video/mp4")),
             ("files", (f"{name}.txt", text.encode(), "text/plain"))]
    resp = await client.post("/api/library/upload", files=files, data={"ai": ai} if ai else {})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_off_the_sheets_texts_go_out_as_they_are_without_listening(
        client, sample_video, ai_on, fake_whisper):
    await reset_mock()
    heard = fake_whisper()
    ai_on.ollama_url = MOCK_BASE
    autocaption.reset_probe_cache()
    assert (await client.get("/api/library")).json()["ai_rewrite"] is False, "apagado por defecto"
    resp = await client.put("/api/settings", json={"hashtags": "#micanal", "webhook_url": ""})
    assert resp.status_code == 200, resp.text

    (item,) = (await _upload(client, sample_video, SHEET))["added"]
    assert item["caption_status"] == "done" and item["caption_source"] == "ficha"
    assert "mismo vídeo" in item["caption_error"]
    tiktok, youtube = item["texts"]["tiktok"], item["texts"]["youtube"]
    assert tiktok["title"] == "¿Se le puede dar leche a un gato?"
    assert tiktok["description"].startswith("Leche para gatos: el plato clásico")
    assert youtube["title"].startswith("¿La leche es mala para los gatos?")
    assert youtube["first_comment"].startswith("¿Qué otro mito")
    # The fixed hashtag of Ajustes first, and five at most.
    assert tiktok["tags"] == ["#micanal", "#gatos", "#datoscuriosos", "#lechedevaca", "#lactosa"]
    assert youtube["tags"] == ["#micanal", "#gatos", "#curiosidadesdegatos", "#lactosa"]
    assert item["transcript"].startswith("El platito de leche")

    # Nothing to write: no Whisper, no Ollama, nothing waiting.
    assert await run_captioner() is False
    assert heard == [] and (await mock_state())["ollama_calls"] == []

    # Each queue takes its platform's texts.
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    (video,) = (await enqueue(client, await default_queue_id(client, account_id), item["id"])).json()["added"]
    assert video["caption"] == tiktok["description"] and video["caption_status"] == "done"
    assert video["first_comment"] == tiktok["first_comment"]


async def test_a_sheet_of_another_length_is_not_used(client, sample_video):
    await reset_mock()
    other = SHEET.replace("· duración: 4,0 s", "· duración: 31,0 s")
    (item,) = (await _upload(client, sample_video, other))["added"]
    assert item["caption_status"] == "failed" and "no parece de este vídeo" in item["caption_error"]
    assert "Leche" not in item["texts"]["tiktok"]["description"]
    rows = (await client.get("/api/library/items", params={"text": "failed"})).json()["items"]
    assert [r["id"] for r in rows] == [item["id"]]


async def test_on_ollama_rewrites_them_and_the_duration_spares_whisper(
        client, sample_video, ai_on, fake_whisper):
    await reset_mock()
    heard = fake_whisper()
    ai_on.ollama_url = MOCK_BASE
    autocaption.reset_probe_cache()
    resp = await client.post("/api/library/ai-texts", json={"enabled": True})
    assert resp.json() == {"ai_rewrite": True}
    assert (await client.get("/api/library")).json()["ai_rewrite"] is True

    (item,) = (await _upload(client, sample_video, SHEET))["added"]
    # While Ollama writes, the sheet's texts stand in.
    assert item["caption_status"] == "pending"
    assert item["texts"]["youtube"]["title"].startswith("¿La leche es mala")

    assert await run_captioner() is True
    row = (await client.get(f"/api/library/{item['id']}")).json()
    assert row["caption_source"] == "ficha+ollama" and "mismo vídeo" in row["caption_error"]
    assert heard == [], "la duración basta: no se escucha el vídeo"
    (call,) = (await mock_state())["ollama_calls"]
    prompt = call["messages"][-1]["content"]
    assert "Textos provisionales de TikTok" in prompt and "leche para gatos" in prompt


async def test_the_upload_says_which_way_and_overrides_the_switch(client, sample_video, tmp_root, ai_on):
    """The page sends the switch as it showed it when the upload started."""
    await reset_mock()
    await client.post("/api/library/ai-texts", json={"enabled": True})
    clip = tmp_root / "other.mp4"
    clip.write_bytes(sample_video.read_bytes() + b"\x00")
    (item,) = (await _upload(client, clip, SHEET, ai="0"))["added"]
    assert item["caption_status"] == "done" and item["caption_source"] == "ficha"


async def test_a_sheet_arriving_later_gives_its_texts_at_once(client, sample_video, ai_on, fake_whisper):
    await reset_mock()
    fake_whisper()
    files = [("files", ("reel_01.mp4", sample_video.read_bytes(), "video/mp4"))]
    (item,) = (await client.post("/api/library/upload", files=files)).json()["added"]
    assert item["caption_status"] == "pending", "sin ficha se escucha, con el interruptor apagado también"
    await run_captioner()

    resp = await client.post("/api/library/upload",
                             files={"files": ("reel_01.txt", SHEET.encode(), "text/plain")})
    assert "usa ahora los textos de la ficha" in resp.json()["notes"][0]
    row = (await client.get(f"/api/library/{item['id']}")).json()
    assert row["caption_source"] == "ficha" and row["caption_status"] == "done"
    assert row["texts"]["tiktok"]["title"] == "¿Se le puede dar leche a un gato?"


async def test_textos_de_la_ficha_brings_them_back(client, sample_video):
    await reset_mock()
    (item,) = (await _upload(client, sample_video, SHEET))["added"]
    await client.patch(f"/api/library/{item['id']}", json={"tiktok": {"description": "Mi texto"}})
    assert (await client.get(f"/api/library/{item['id']}")).json()["caption_source"] == "manual"

    resp = await client.post("/api/library/bulk", json={"action": "sheet", "ids": [item["id"]]})
    assert resp.status_code == 200 and resp.json()["done"] == 1
    row = (await client.get(f"/api/library/{item['id']}")).json()
    assert row["caption_source"] == "ficha" and row["texts"]["tiktok"]["description"].startswith("Leche")

    files = [("files", ("suelto.mp4", sample_video.read_bytes() + b"\x01", "video/mp4"))]
    (plain,) = (await client.post("/api/library/upload", files=files)).json()["added"]
    resp = await client.post("/api/library/bulk", json={"action": "sheet", "ids": [plain["id"]]})
    assert resp.json()["done"] == 0 and "no tiene ficha" in resp.json()["problems"][0]


async def test_files_copied_by_hand_follow_the_stored_switch(client, sample_video, tmp_root):
    await reset_mock()
    root = tmp_root / "library"
    (root / "reel_07_gatos_leche.mp4").write_bytes(sample_video.read_bytes() + b"\x02")
    (root / "reel_07_gatos_leche.txt").write_text(SHEET, encoding="utf-8")
    assert (await client.post("/api/library/scan")).json()["added"] == 1
    (row,) = (await client.get("/api/library/items", params={"folder": "gatos"})).json()["items"]
    assert row["caption_source"] == "ficha" and row["texts"]["youtube"]["title"].startswith("¿La leche")
