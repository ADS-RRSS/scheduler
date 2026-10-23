"""The editor's review: the generator's rules on a text written by hand, and its helpers."""
from __future__ import annotations

from pathlib import Path

from app.services import textcheck as tc
from tests.helpers import MOCK_BASE, configure_mock, reset_mock, upload_to_library
from tests.test_autocaption import ai_on  # noqa: F401 - fixture

SERIES = "pensamientos-estoicos"

SHEET = """No nos perturban las cosas, sino los juicios que hacemos sobre ellas. Epicteto lo repetía en su Think Week.

—
No nos perturban las cosas, sino los juicios que hacemos sobre ellas.
· portada: El juicio, no el hecho

— pie de publicación —
El juicio, no el hecho · estoicismo · Epicteto · 2/50
#estoicismo #filosofia #epicteto

— comentario fijado —
· ¿Qué juicio te está pesando hoy?

· verificación: publicable
"""


def codes(report) -> set[str]:
    return {issue.code for issue in report.issues}


def texts(**kw) -> tc.Texts:
    base = {"title": "El juicio, no el hecho: la lección de Epicteto",
            "description": "Por qué te pesa más lo que piensas que lo que pasa\n"
                           "Epicteto separaba el hecho del juicio que haces de él.\n"
                           "¿Qué juicio te pesa hoy?",
            "tags": ["#estoicismo", "#filosofia", "#epicteto"], "first_comment": ""}
    base.update(kw)
    return tc.Texts.of(**base)


# --------------------------------------------------------------------------- #
# The review, pure
# --------------------------------------------------------------------------- #

def test_a_clean_text_has_nothing_to_say():
    report = tc.check(texts(), "tiktok")
    assert report.issues == []
    stats = report.as_dict()["stats"]
    assert stats["tags_counted"] == 3 and stats["hook_chars"] == len(
        "Por qué te pesa más lo que piensas que lo que pasa")


def test_the_generator_rules_apply_to_a_hand_written_text():
    report = tc.check(texts(description="Sabías que el juicio pesa? · estoicismo · Epicteto · 2/50\n"
                                        "Algo que decir con the mindset"), "tiktok")
    found = codes(report)
    assert {"hook_stock", "open_marks", "series_counter", "keyword_list", "desc_english",
            "no_question"} <= found
    levels = {i.code: i.level for i in report.issues}
    assert levels["desc_english"] == "warn" and levels["no_question"] == "tip"


def test_english_the_video_itself_says_is_allowed():
    ctx = tc.Context(source="Bill Gates y su Think Week")
    report = tc.check(texts(description="La Think Week de Bill Gates\n¿La harías?"), "tiktok", ctx)
    assert "desc_english" not in codes(report)


def test_a_date_at_the_end_of_a_line_is_not_a_series_counter():
    assert "series_counter" not in codes(tc.check(texts(description="Nos vemos el 3/10\n¿Vienes?")))
    assert "series_counter" in codes(tc.check(texts(description="Serie estoica · 3/10\n¿Vienes?")))
    assert "series_counter" in codes(tc.check(texts(description="7/50\n¿Vienes?")))


def test_tiktok_counts_five_hashtags_and_the_fixed_ones_take_their_share():
    ctx = tc.Context(fixed=("#mimarca", "#otramarca"))
    report = tc.check(texts(tags=["#estoicismo", "#filosofia", "#epicteto", "#calma"]), "tiktok", ctx)
    preview = report.preview
    assert preview["tags"][-2:] == ["#mimarca", "#otramarca"]
    assert preview["counted_tags"] == ["#estoicismo", "#filosofia", "#epicteto", "#calma", "#mimarca"]
    assert preview["ignored_tags"] == ["#otramarca"]
    over = next(i for i in report.issues if i.code == "tags_over_limit")
    assert "#otramarca" in over.message and "fijos" in over.message


def test_a_tag_in_the_text_and_in_the_list_would_go_out_twice():
    report = tc.check(texts(description="El juicio #estoicismo\n¿Y tú?"), "tiktok")
    assert "inline_tag_duplicate" in codes(report)
    assert report.preview["caption"].count("#estoicismo") == 2


def test_bad_tags_are_named_by_what_is_wrong_with_them():
    report = tc.check(texts(tags=["#fyp", "#selfcare", "#x", "#tiempo独自", "#Estoicismo",
                                  "estoicismo"]), "tiktok")
    by_code = {i.code: i for i in report.issues}
    assert by_code["tag_generic"].level == "tip" and "#fyp" in by_code["tag_generic"].message
    assert "#autocuidado" in by_code["tag_english"].message
    assert "#x" in by_code["tag_invalid"].message
    assert by_code["tag_foreign"].level == "error" and "#tiempo独自" in by_code["tag_foreign"].message
    assert "tag_duplicate" in by_code
    assert all(by_code[c].fixable for c in ("tag_generic", "tag_english", "tag_invalid",
                                            "tag_foreign", "tag_duplicate"))


def test_youtube_needs_a_title_and_has_its_own_limits():
    report = tc.check(texts(title=""), "youtube")
    assert next(i for i in report.issues if i.code == "title_missing").level == "error"
    long = tc.check(texts(title="x" * 101, tags=[f"#etiqueta{i:02d}muylarga" for i in range(30)]),
                    "youtube")
    assert {"title_long", "youtube_tags_long"} <= codes(long)
    assert long.as_dict()["stats"]["youtube_tags_chars"] > tc.YOUTUBE_TAGS_MAX_CHARS
    # TikTok's own ceiling on the pinned comment does not apply on YouTube.
    comment = "¿" + "a" * 160 + "?"
    assert "comment_long" in codes(tc.check(texts(first_comment=comment), "tiktok"))
    assert "comment_long" not in codes(tc.check(texts(first_comment=comment), "youtube"))


def test_the_first_line_is_what_shows_before_mas():
    hook = "Una primera línea larguísima " * 5
    report = tc.check(texts(description=f"{hook}\n¿Y tú?"), "tiktok")
    assert "hook_long" in codes(report)
    assert len(report.preview["visible"]) == tc.FOLD_CHARS
    short = tc.check(texts(description="Mira\n¿Y tú?"), "tiktok")
    assert "hook_short" in codes(short)


def test_the_caption_over_2200_is_an_error():
    report = tc.check(texts(description="Frase. " * 400 + "\n¿Y tú?"), "tiktok")
    assert next(i for i in report.issues if i.code == "caption_long").level == "error"


def test_unknown_fields_and_open_braces_are_flagged():
    report = tc.check(texts(description="Hoy [fecha] con [autor] {hola|buenas\n¿Y tú?"), "tiktok")
    assert {"placeholder_unknown", "spintax_unbalanced"} <= codes(report)
    unknown = next(i for i in report.issues if i.code == "placeholder_unknown")
    assert "[autor]" in unknown.message and "[fecha]" not in unknown.message.split(".")[0]


def test_same_opening_as_another_video_of_the_series():
    ctx = tc.Context(siblings=("Por qué te pesa más lo que piensas que lo que ocurre",))
    assert "same_opening" in codes(tc.check(texts(), "tiktok", ctx))


def test_the_preview_resolves_variants_the_same_way_every_time():
    t = texts(description="{Hola|Buenas|Qué tal} a todos\n¿Y tú?")
    first = tc.check(t, "tiktok").preview["caption"]
    assert all(tc.check(t, "tiktok").preview["caption"] == first for _ in range(5))
    assert "{" not in first


# --------------------------------------------------------------------------- #
# The fixes
# --------------------------------------------------------------------------- #

def test_autofix_repairs_without_rewriting():
    before = texts(
        title="El juicio #estoicismo",
        description="Sabías que el juicio pesa? #estoicismo #calma\nSerie estoica · 2/50\n\n\nQue opinas?",
        tags=["#estoicismo", "#fyp", "#selfcare", "#x", "#Filosofia", "filosofia"],
    )
    after, changes = tc.autofix(before)
    assert after.title == "El juicio"
    assert after.description == "¿Sabías que el juicio pesa?\nSerie estoica\n\n¿Que opinas?"
    # Typed in the text first, so first in the list; English translated; the rest cleaned.
    assert after.tags == ("#estoicismo", "#calma", "#autocuidado", "#filosofia")
    assert len(changes) >= 6
    # Fixing twice changes nothing more.
    again, more = tc.autofix(after)
    assert again == after and more == []
    remaining = codes(tc.check(after, "tiktok"))
    assert not remaining & {"open_marks", "series_counter", "inline_tag_duplicate", "tag_generic",
                            "tag_english", "tag_invalid", "tag_duplicate", "title_hashtags"}


def test_suggestions_skip_what_is_there_and_say_why():
    out = tc.suggestions(
        current=["#estoicismo"], fixed=["#mimarca"],
        series=[("#epicteto", 12), ("#estoicismo", 30), ("#fyp", 40)],
        proven=[{"tag": "#filosofia", "posts": 5, "views_avg": 4300, "trusted": True},
                {"tag": "#suerte", "posts": 1, "views_avg": 90000, "trusted": False}],
        topic=["#mimarca", "#calma"],
    )
    assert [(s["tag"], s["source"]) for s in out] == [
        ("#filosofia", "funcionan"), ("#epicteto", "serie"), ("#calma", "tema")]
    assert out[0]["detail"].startswith("4.300 reproducciones")


# --------------------------------------------------------------------------- #
# Through the API
# --------------------------------------------------------------------------- #

async def _sheet_item(client, sample_video: Path, tmp_root: Path, number: int = 2) -> dict:
    name = f"reel_{number:03d}_{SERIES}_el-juicio-no-el-hecho.mp4"
    clip = tmp_root / name
    clip.write_bytes(sample_video.read_bytes() + bytes([number]))
    resp = await client.post("/api/library/upload", files=[
        ("files", (name, clip.read_bytes(), "video/mp4")),
        ("files", (Path(name).with_suffix(".txt").name, SHEET.encode(), "text/plain")),
    ])
    assert resp.status_code == 200, resp.text
    return resp.json()["added"][0]


async def test_check_reads_the_video_for_context(client, sample_video, tmp_root):
    item = await _sheet_item(client, sample_video, tmp_root)
    body = {"platform": "tiktok", "library_id": item["id"], "title": "",
            "description": "Lo que aprendió en su Think Week\n¿La harías?", "tags": ["#estoicismo"]}
    report = (await client.post("/api/text/check", json=body)).json()
    found = {i["code"] for i in report["issues"]}
    # «Think Week» is in the sheet's script: the video says it, so it is no mistake.
    assert "desc_english" not in found
    assert "tags_few" in found
    assert report["preview"]["caption"].endswith("#estoicismo")
    assert report["counts"]["tip"] >= 1

    fixed = (await client.post("/api/text/fix", json={
        **body, "description": "Que harías? #calma", "tags": ["#fyp"]})).json()
    assert fixed["texts"]["description"] == "¿Que harías?"
    assert fixed["texts"]["tags"] == ["#calma"]
    assert fixed["changes"] and "open_marks" not in {i["code"] for i in fixed["report"]["issues"]}


async def test_check_answers_404_for_an_unknown_video(client):
    resp = await client.post("/api/text/check", json={"library_id": 999, "description": "x"})
    assert resp.status_code == 404


async def test_hashtag_suggestions_come_from_the_series(client, sample_video, tmp_root):
    first = await _sheet_item(client, sample_video, tmp_root, 2)
    await _sheet_item(client, sample_video, tmp_root, 3)
    resp = await client.get("/api/text/hashtags", params={
        "platform": "tiktok", "library_id": first["id"], "current": "#estoicismo"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    series = [s["tag"] for s in data["suggestions"] if s["source"] == "serie"]
    assert "#filosofia" in series and "#estoicismo" not in series
    assert data["max"] == 5


async def test_suggest_writes_a_draft_without_saving(client, sample_video, tmp_root, ai_on):
    await reset_mock()
    item = await _sheet_item(client, sample_video, tmp_root)
    before = (await client.get(f"/api/library/{item['id']}")).json()["texts"]
    ai_on.ollama_url = MOCK_BASE
    await configure_mock(ollama_caption="El juicio pesa más que el hecho, según Epicteto")
    resp = await client.post("/api/text/suggest", json={"platform": "tiktok",
                                                        "library_id": item["id"]})
    assert resp.status_code == 200, resp.text
    draft = resp.json()
    assert draft["description"].startswith("El juicio pesa más que el hecho")
    assert draft["tags"] and draft["source"] == "ficha"
    after = (await client.get(f"/api/library/{item['id']}")).json()["texts"]
    assert after == before


async def test_suggest_explains_why_it_cannot(client, sample_video, tmp_root, ai_on):
    plain = await upload_to_library(client, sample_video, caption="Texto a mano", name="suelto.mp4")
    resp = await client.post("/api/text/suggest", json={"library_id": plain["id"]})
    assert resp.status_code == 409 and "transcripción" in resp.json()["detail"]
    item = await _sheet_item(client, sample_video, tmp_root)
    ai_on.ollama_url = "http://127.0.0.1:9"
    resp = await client.post("/api/text/suggest", json={"library_id": item["id"]})
    assert resp.status_code == 409


async def test_suggest_improves_a_hand_written_text(client, sample_video, ai_on):
    await reset_mock()
    plain = await upload_to_library(client, sample_video, caption="Texto a mano", name="suelto.mp4")
    ai_on.ollama_url = MOCK_BASE
    resp = await client.post("/api/text/suggest", json={
        "library_id": plain["id"], "title": "Mi título", "description": "Madrugar me cambió el día"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["source"] == "tu texto"
    from tests.helpers import mock_state
    prompt = (await mock_state())["ollama_calls"][-1]["messages"][-1]["content"]
    assert "Madrugar me cambió el día" in prompt


def test_the_title_gets_its_marks_too():
    report = tc.check(texts(title="Qué juicio te pesa?"), "youtube")
    assert "title_marks" in codes(report)
    fixed, changes = tc.autofix(texts(title="Qué juicio te pesa?"))
    assert fixed.title == "¿Qué juicio te pesa?" and any("título" in c for c in changes)
