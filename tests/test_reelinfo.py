"""The .txt sheet that travels with a reel, and the description written from it.

The sheet below is a real one from ContentBuilder (reel 1 of «microcomportamientos
psicológicos que delatan…»). Whisper and Ollama are the fakes of
test_autocaption.py: the check that the sheet belongs to the video runs on
whatever ``fake_whisper`` says it heard.
"""
from __future__ import annotations

import pytest

from app.services import autocaption, reelinfo
from tests.helpers import (
    MOCK_BASE,
    configure_mock,
    default_queue_id,
    first_video,
    make_account,
    mock_state,
    put_schedule,
    reset_mock,
    upload_to_queue,
)
from tests.test_autocaption import ai_on, fake_whisper, run_captioner  # noqa: F401 - fixtures

SHEET = """Mary Ainsworth separaba unos minutos a bebés de sus madres y observaba, sobre todo, el regreso. ¿Qué hace tu pareja en los primeros segundos cuando vuelves a casa? En su prueba, cuánto lloraba el bebé al quedarse solo decía poco. Lo revelador era el reencuentro: si buscaba a su madre, la evitaba o se enfadaba. Con los adultos pasa algo parecido, y casi nadie lo mira. Escribe REGRESO si tu pareja ni levanta la vista cuando llegas.

—
Mary Ainsworth separaba unos minutos a bebés de sus madres y observaba, sobre todo, el regreso.
· portada: El reencuentro delata el apego

— pie de publicación —
El reencuentro delata el apego · apego ansioso pareja · señales de desinterés
microcomportamientos psicológicos que delatan desinterés silencioso o manipulación sutil en una relación, explicados desde la psicología del apego · 1/50
#psicologiadelapego #apegoansioso #apegoevitativo #relacionessanas #manipulacionemocional

— comentario fijado —
· ¿Qué hace tu pareja cuando vuelves a casa?
· ¿Te fijas más en las despedidas o en los regresos?

· idea: Lo que revela el reencuentro
· verificación: ⚠ revisar a mano — Conviene confirmar el año del experimento
"""
# What Whisper hears in the first seconds of that reel, and of another one.
HEARD = ("Mary Ainsworth separaba unos minutos a bebés de sus madres y observaba sobre todo "
         "el regreso. ¿Qué hace tu pareja en los primeros segundos cuando vuelves a casa?")
OTHER = ("Le enseñas un pájaro raro en la ventana y tu pareja no contesta. Parece una "
         "tontería, pero ahí acaba de pasar algo medible.")


# --------------------------------------------------------------------------- #
# Reading the sheet
# --------------------------------------------------------------------------- #

def test_a_content_builder_sheet_is_read_field_by_field():
    info = reelinfo.parse(SHEET)
    assert info.script.startswith("Mary Ainsworth") and info.script.endswith("cuando llegas.")
    assert info.hook.startswith("Mary Ainsworth separaba")
    assert info.title == "El reencuentro delata el apego"
    assert info.caption_lines[0].startswith("El reencuentro delata el apego · apego")
    assert info.caption_lines[1].endswith("· 1/50")
    assert info.hashtags == ("#psicologiadelapego", "#apegoansioso", "#apegoevitativo",
                             "#relacionessanas", "#manipulacionemocional")
    assert info.pins == ("¿Qué hace tu pareja cuando vuelves a casa?",
                         "¿Te fijas más en las despedidas o en los regresos?")
    assert info.idea == "Lo que revela el reencuentro"
    assert info.verdict == "revisar a mano" and info.needs_review
    assert info.verdict_notes == "Conviene confirmar el año del experimento"


def test_a_caption_written_by_hand_is_not_a_sheet():
    for text in ("Mi texto {a|b} [hashtags]", "Una línea\n\nOtra línea #tag",
                 "Hoy: tres ideas · una por día"):
        assert not reelinfo.is_sheet(text) and reelinfo.parse(text) is None


def test_the_audio_tells_which_sheet_belongs_to_the_video():
    script = reelinfo.parse(SHEET).script
    assert reelinfo.script_match(script, HEARD) > 0.8
    assert reelinfo.script_match(script, OTHER) < 0.3
    assert reelinfo.script_match(script, "música") is None, "sin voz no se puede decir"


def test_without_a_model_the_sheet_gives_its_title_and_first_question():
    draft = autocaption.sheet_fallback(reelinfo.parse(SHEET))
    assert draft.caption == ("El reencuentro delata el apego\n"
                             "¿Qué hace tu pareja cuando vuelves a casa?")
    assert draft.hashtags[0] == "#psicologiadelapego"


# --------------------------------------------------------------------------- #
# Through the library and into a queue
# --------------------------------------------------------------------------- #

async def upload_pair(client, account_id: int, video, text: str | None, *,
                      name: str = "reel_01", ai: bool | None = None) -> dict:
    """Upload a video and its .txt in one go, as the library dropzone does,
    then add it to the account's queue. Returns the queued video. ``ai`` is
    the switch «Reescribir los textos con IA» for this upload."""
    files = [("files", (f"{name}.mp4", video.read_bytes(), "video/mp4"))]
    if text is not None:
        files.append(("files", (f"{name}.txt", text.encode(), "text/plain")))
    data = {} if ai is None else {"ai": "1" if ai else "0"}
    resp = await client.post("/api/library/upload", files=files, data=data)
    assert resp.status_code == 200, resp.text
    (item,) = resp.json()["added"]
    queued = await client.post(f"/api/schedules/{await default_queue_id(client, account_id)}/videos",
                               json={"library_ids": [item["id"]]})
    (added,) = queued.json()["added"]
    return added


async def library_row(client) -> dict:
    (item,) = (await client.get("/api/library/items", params={"q": "reel_01"})).json()["items"]
    return item


async def test_a_sheet_uploaded_with_its_video_is_the_base_of_the_description(
        client, sample_video, ai_on, fake_whisper, tmp_root):
    await reset_mock()
    heard = fake_whisper(text=HEARD)
    ai_on.ollama_url = MOCK_BASE
    autocaption.reset_probe_cache()
    account_id = await make_account(client)
    await put_schedule(client, account_id)

    video = await upload_pair(client, account_id, sample_video, SHEET, ai=True)
    # Not published as is: it is context, and the text is still to be written.
    assert video["caption_status"] == "pending" and "separaba unos minutos" not in video["caption"]
    sheet_path = tmp_root / "library" / "varios" / "reel_01.txt"
    assert sheet_path.read_text(encoding="utf-8") == SHEET

    assert await run_captioner() is True
    row = await library_row(client)
    assert row["caption_source"] == "ficha+ollama"
    assert row["texts"]["tiktok"]["description"].startswith("Cinco hábitos que sí cambian el día")
    assert row["transcript"].startswith("Mary Ainsworth"), "el guion, no lo que oyó Whisper"
    assert "comprobada con el audio" in row["caption_error"]
    assert "revisar el guion" in row["caption_error"]
    item = await first_video(client)
    assert item["caption"] == row["texts"]["tiktok"]["description"]
    assert "revisar el guion" in item["caption_error"]
    # Whisper only listened to the opening.
    assert heard == [ai_on.caption_sheet_check_seconds]

    (call,) = (await mock_state())["ollama_calls"]
    assert "ficha" in call["messages"][0]["content"]
    prompt = call["messages"][-1]["content"]
    assert "El reencuentro delata el apego" in prompt and "1/50" in prompt
    assert "#apegoevitativo" in prompt and "¿Te fijas más en las despedidas" in prompt

    (event,) = (await client.get("/api/events?kind=library_caption_generated")).json()["events"]
    assert event["level"] == "warn" and "Cambios: quitado el contador" in event["message"]


async def test_a_sheet_about_another_video_is_ignored(client, sample_video, ai_on, fake_whisper):
    await reset_mock()
    heard = fake_whisper(text=OTHER)
    ai_on.ollama_url = MOCK_BASE
    autocaption.reset_probe_cache()
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    await upload_pair(client, account_id, sample_video, SHEET, ai=True)

    await run_captioner()
    row = await library_row(client)
    assert row["caption_source"] == "whisper+ollama"
    assert "no parece de este vídeo" in row["caption_error"]
    assert row["transcript"] == OTHER
    # The opening first, then the whole video for the path without a sheet.
    assert heard == [ai_on.caption_sheet_check_seconds, None]
    (call,) = (await mock_state())["ollama_calls"]
    assert "ficha" not in call["messages"][0]["content"]


async def test_a_plain_txt_uploaded_with_its_video_is_still_the_caption(client, sample_video, ai_on):
    await reset_mock()
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    video = await upload_pair(client, account_id, sample_video, "Mi texto de siempre [hashtags]")
    assert video["caption"] == "Mi texto de siempre [hashtags]"
    assert video["caption_source"] == "biblioteca" and video["caption_status"] == "none"
    row = await library_row(client)
    assert row["caption_source"] == "manual"
    assert row["texts"]["youtube"]["description"] == "Mi texto de siempre [hashtags]"
    assert row["texts"]["youtube"]["title"] == "Mi texto de siempre [hashtags]"


async def test_a_txt_uploaded_later_finds_its_video_in_the_library(
        client, sample_video, ai_on, fake_whisper):
    await reset_mock()
    fake_whisper(text=HEARD)
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    await upload_pair(client, account_id, sample_video, None)
    await run_captioner()
    assert (await library_row(client))["caption_source"] == "whisper"

    resp = await client.post("/api/library/upload",
                             files={"files": ("reel_01.txt", SHEET.encode(), "text/plain")},
                             data={"ai": "1"})
    body = resp.json()
    assert not body["problems"] and "ficha" in body["notes"][0]
    assert (await library_row(client))["caption_status"] == "pending"
    assert (await first_video(client))["caption_status"] == "pending"
    await run_captioner()
    assert (await library_row(client))["caption_source"] == "ficha"
    assert (await first_video(client))["caption"].startswith("El reencuentro delata el apego")

    stray = await client.post("/api/library/upload",
                              files={"files": ("otro.txt", b"hola", "text/plain")})
    assert "no hay ningún vídeo" in stray.json()["problems"][0]


async def test_with_automatic_texts_off_a_sheet_still_beats_an_empty_caption(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    video = await upload_pair(client, account_id, sample_video, SHEET)
    assert video["caption"] == ("El reencuentro delata el apego\n"
                                "¿Qué hace tu pareja cuando vuelves a casa?")
    assert video["caption_source"] == "biblioteca" and video["caption_status"] == "done"
    assert video["auto_hashtags"][0] == "#psicologiadelapego"
    assert len(video["auto_hashtags"]) == 5
    row = await library_row(client)
    assert row["caption_source"] == "ficha" and "ficha antigua" in row["caption_error"]
    assert "revisar el guion" in row["caption_error"]


async def test_a_delegated_video_not_yet_in_zernio_still_gets_its_text(
        client, sample_video, ai_on, fake_whisper):
    """Before, the captioner skipped delegated videos: the handover waited
    ten minutes for a text nobody was writing and sent it without one."""
    await reset_mock()
    fake_whisper()
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    queue_id = await default_queue_id(client, account_id)
    await upload_to_queue(client, queue_id, sample_video, caption="")
    assert (await client.post(f"/api/schedules/{queue_id}/delegate", json={})).status_code == 200
    item = await first_video(client)
    assert item["status"] == "DELEGATED" and item["caption_status"] == "pending"

    assert await run_captioner() is True
    item = await first_video(client)
    assert item["caption_status"] == "done" and item["caption"]
    again = await client.post(f"/api/videos/{item['id']}/caption/regenerate")
    assert again.status_code == 200, again.text


@pytest.mark.parametrize("hook, fixed", [
    ("Silencio comparte: confianza o distancia?", "¿Silencio comparte: confianza o distancia?"),
    ("Resuelves problemas en la ducha? 🤔", "¿Resuelves problemas en la ducha? 🤔"),
    ("¡Despierta del pasado!", "¡Despierta del pasado!"),
])
def test_opening_marks_are_added(hook, fixed):
    assert autocaption.tidy(autocaption.Draft(hook=hook)).hook == fixed
