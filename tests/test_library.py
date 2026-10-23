"""The shared library: one file per video, texts per platform, queues take from it."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.services import autocaption, library
from tests.helpers import (
    ACCOUNT_1,
    ACCOUNT_2,
    MOCK_BASE,
    account_entry,
    add_queue,
    default_queue_id,
    drain,
    enqueue,
    make_account,
    mock_state,
    put_schedule,
    reset_mock,
    upload_to_library,
)

YOUTUBE = "acc_youtube_1"
SERIES = "pensamientos-estoicos"
REEL = f"reel_002_{SERIES}_el-juicio-no-el-hecho.mp4"

SHEET = """No nos perturban las cosas, sino los juicios que hacemos sobre ellas. Epicteto lo repetía.

—
No nos perturban las cosas, sino los juicios que hacemos sobre ellas.
· portada: El juicio, no el hecho

— pie de publicación —
El juicio, no el hecho · estoicismo · Epicteto · 2/50
Pensamientos estoicos para tu día a día · 2/50
#estoicismo #filosofia #epicteto

— comentario fijado —
· ¿Qué juicio te está pesando hoy?
· ¿Te ha pasado alguna vez?

· idea: separar el hecho del juicio
· verificación: publicable
"""


def _variant(sample: Path, tmp: Path, name: str, extra: bytes = b"") -> Path:
    """The sample clip under another name, optionally with other bytes (another checksum)."""
    target = tmp / name
    target.write_bytes(sample.read_bytes() + extra)
    return target


async def _upload(client, *files: tuple[str, bytes, str], folder: str = "", into: str = "",
                  ai: bool | None = None) -> dict:
    data = {"folder": folder, "into": into} | ({} if ai is None else {"ai": "1" if ai else "0"})
    resp = await client.post("/api/library/upload", files=[("files", f) for f in files], data=data)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _items(client, **params) -> list[dict]:
    return (await client.get("/api/library/items", params=params)).json()["items"]


# --------------------------------------------------------------------------- #
# Folders
# --------------------------------------------------------------------------- #

def test_the_folder_is_the_series_in_the_file_name():
    assert library.folder_for(
        "reel_01_microcomportamientos-psicologicos-que-delatan_lo-que-revela-el-reencuentro.mp4"
    ) == "microcomportamientos-psicologicos-que-delatan"
    assert library.folder_for("reel_002_pensamientos-estoicos_el-juicio-no-el-hecho-1.mp4") \
        == "pensamientos-estoicos"
    assert library.folder_for("REEL_7_Series-Ñandú_tema.mp4") == "series-nandu"
    # A name that says nothing: the folder it came from, or the default one.
    assert library.folder_for("IMG_2034.mp4", hint="Viaje a Roma") == "viaje-a-roma"
    assert library.folder_for("IMG_2034.mp4") == library.DEFAULT_FOLDER


# --------------------------------------------------------------------------- #
# Uploading
# --------------------------------------------------------------------------- #

async def test_a_folder_upload_files_each_video_under_its_series_with_its_sheet(
        client, sample_video, tmp_root):
    await reset_mock()
    res = await _upload(client, (REEL, sample_video.read_bytes(), "video/mp4"),
                        (Path(REEL).with_suffix(".txt").name, SHEET.encode(), "text/plain"),
                        folder="lo que sea")
    (item,) = res["added"]
    path = Path(item["file_path"])
    assert path == tmp_root / "library" / SERIES / REEL
    assert path.exists() and path.with_suffix(".txt").read_text(encoding="utf-8") == SHEET
    assert item["folder"] == SERIES
    # With automatic texts off, the sheet already gives both platforms something.
    for platform in ("tiktok", "youtube"):
        texts = item["texts"][platform]
        assert texts["title"] == "El juicio, no el hecho"
        assert "#estoicismo" in texts["tags"]
        assert "¿Qué juicio te está pesando hoy?" in texts["first_comment"]
    assert item["caption_source"] == "ficha"
    assert not (tmp_root / "library" / library.INCOMING_DIR / REEL).exists()

    overview = (await client.get("/api/library")).json()
    assert overview["total"] == 1 and overview["folders"][0]["folder"] == SERIES


async def test_folders_uploaded_together_can_all_go_to_one_folder(client, sample_video, tmp_root):
    """«Varias carpetas…»: ``into`` beats the series in the name and the folder uploaded."""
    await reset_mock()
    first = await upload_to_library(client, sample_video, name=REEL)
    other = _variant(sample_video, tmp_root, "otro.mp4", b"\0")
    res = await _upload(client, ("IMG_2034.mp4", other.read_bytes(), "video/mp4"),
                        ("IMG_2034.txt", b"Roma", "text/plain"),
                        folder="Viaje a Roma", into="Mis Mezclas")
    (item,) = res["added"]
    assert item["folder"] == "mis-mezclas"
    assert Path(item["file_path"]) == tmp_root / "library" / "mis-mezclas" / "IMG_2034.mp4"
    assert Path(item["file_path"]).with_suffix(".txt").read_text(encoding="utf-8") == "Roma"
    # Into a folder that exists, whatever the series says.
    third = _variant(sample_video, tmp_root, "tercero.mp4", b"\0\0")
    res = await _upload(client, (REEL.replace("002", "003"), third.read_bytes(), "video/mp4"),
                        into="mis-mezclas")
    assert res["added"][0]["folder"] == "mis-mezclas"
    # A video already in the library stays where it is.
    res = await _upload(client, ("copia.mp4", sample_video.read_bytes(), "video/mp4"), into="mis-mezclas")
    assert res["added"] == [] and res["duplicates"][0]["folder"] == first["folder"] == SERIES
    # Without ``into``, as always.
    fourth = _variant(sample_video, tmp_root, "cuarto.mp4", b"\0\0\0")
    res = await _upload(client, (REEL.replace("002", "004"), fourth.read_bytes(), "video/mp4"),
                        folder="Lo que sea")
    assert res["added"][0]["folder"] == SERIES

    resp = await client.post("/api/library/upload", data={"into": "¡¿?!"},
                             files=[("files", ("x.mp4", b"x", "video/mp4"))])
    assert resp.status_code == 422


async def test_the_same_bytes_under_another_name_stay_one_file(client, sample_video, tmp_root):
    await reset_mock()
    first = await upload_to_library(client, sample_video, name=REEL)
    res = await _upload(client, ("reel_002_pensamientos-estoicos_el-juicio-no-el-hecho-1.mp4",
                                 sample_video.read_bytes(), "video/mp4"))
    assert res["added"] == []
    assert res["duplicates"][0]["library_id"] == first["id"]
    assert sorted(p.name for p in (tmp_root / "library").rglob("*.mp4")) == [REEL]


async def test_a_txt_uploaded_later_finds_its_video_in_the_library(client, sample_video):
    await reset_mock()
    item = await upload_to_library(client, sample_video, name=REEL)
    res = await _upload(client, (Path(REEL).with_suffix(".txt").name, b"Mi texto", "text/plain"))
    assert res["notes"] and "usa ahora el texto" in res["notes"][0]
    (row,) = await _items(client, folder=SERIES)
    assert row["id"] == item["id"]
    assert row["texts"]["tiktok"]["description"] == "Mi texto"
    assert row["texts"]["youtube"]["title"] == "Mi texto"
    assert row["caption_source"] == "manual"

    stray = await _upload(client, ("otro.txt", b"hola", "text/plain"))
    assert stray["problems"] and "ningún vídeo" in stray["problems"][0]


COVER = b"\xff\xd8\xff\xe0 portada \xff\xd9"


async def test_a_cover_travels_with_its_video_under_the_videos_name(client, sample_video, tmp_root):
    await reset_mock()
    # ContentBuilder names it <video>_portada.jpg; the library keeps <video>.jpg.
    res = await _upload(client, (REEL, sample_video.read_bytes(), "video/mp4"),
                        (Path(REEL).stem + "_portada.jpg", COVER, "image/jpeg"))
    (item,) = res["added"]
    assert item["has_cover"]
    assert Path(item["file_path"]).with_suffix(".jpg").read_bytes() == COVER
    cover = await client.get(f"/api/library/{item['id']}/cover")
    assert cover.status_code == 200 and cover.content == COVER
    assert cover.headers["content-type"] == "image/jpeg"

    # One uploaded later, on its own, replaces it.
    res = await _upload(client, (Path(REEL).stem + ".jpg", b"otra", "image/jpeg"))
    assert res["notes"] and "sustituye" in res["notes"][0]
    assert (await client.get(f"/api/library/{item['id']}/cover")).content == b"otra"

    stray = await _upload(client, ("otro_portada.jpg", COVER, "image/jpeg"))
    assert stray["problems"] and "ningún vídeo" in stray["problems"][0]

    await client.delete(f"/api/library/{item['id']}")
    assert not list((tmp_root / "library" / SERIES).glob("*.jpg"))


async def test_a_video_without_a_cover_says_so(client, sample_video):
    await reset_mock()
    item = await upload_to_library(client, sample_video, name=REEL)
    assert item["has_cover"] is False
    assert (await client.get(f"/api/library/{item['id']}/cover")).status_code == 404


async def test_a_broken_file_is_listed_but_cannot_be_queued(client):
    await reset_mock()
    account_id = await make_account(client)
    res = await _upload(client, ("roto.mp4", b"esto no es un video", "video/mp4"))
    (item,) = res["added"]
    assert item["preflight_errors"]
    folders = (await client.get("/api/library")).json()["folders"]
    assert folders[0]["invalid"] == 1

    resp = await enqueue(client, await default_queue_id(client, account_id), item["id"])
    assert resp.json()["added"] == [] and "validación" in resp.json()["problems"][0]


async def test_uploads_are_streamed_to_disk(client, sample_video):
    await reset_mock()
    with sample_video.open("rb") as fh:
        resp = await client.post("/api/library/upload",
                                 files={"files": ("stream.mp4", fh, "video/mp4")})
    (item,) = resp.json()["added"]
    assert Path(item["file_path"]).stat().st_size == sample_video.stat().st_size


# --------------------------------------------------------------------------- #
# The watch folder
# --------------------------------------------------------------------------- #

async def test_a_video_copied_into_the_library_by_hand_is_filed_and_registered(
        client, sample_video, tmp_root):
    from app.db import session_scope

    dropped = tmp_root / "library" / REEL
    dropped.write_bytes(sample_video.read_bytes())
    dropped.with_suffix(".txt").write_text("Texto a mano", encoding="utf-8")
    dropped.with_name(dropped.stem + "_portada.jpg").write_bytes(COVER)

    with session_scope() as session:
        # Still being copied: left alone.
        assert library.scan(session, tmp_root / "library", min_age_seconds=60) == []
        added = library.scan(session, tmp_root / "library", min_age_seconds=0)
        assert [a.folder for a in added] == [SERIES]
    filed = tmp_root / "library" / SERIES / REEL
    assert filed.exists() and filed.with_suffix(".txt").exists() and not dropped.exists()
    assert filed.with_name(filed.stem + "_portada.jpg").read_bytes() == COVER

    # The same bytes dropped again under another name are reported once, not re-read.
    twin = tmp_root / "library" / SERIES / "copia.mp4"
    twin.write_bytes(sample_video.read_bytes())
    old = time.time() - 120
    os.utime(twin, (old, old))
    with session_scope() as session:
        assert library.scan(session, tmp_root / "library", min_age_seconds=5) == []
        assert library.scan(session, tmp_root / "library", min_age_seconds=5) == []
    events = (await client.get("/api/events?kind=library_duplicate")).json()["events"]
    assert len(events) == 1


# --------------------------------------------------------------------------- #
# Into the queues
# --------------------------------------------------------------------------- #

async def _connect_both(client) -> tuple[int, int]:
    resp = await client.post("/api/accounts", json={
        "api_key": "zernio-test-key", "account_ids": [ACCOUNT_1, YOUTUBE]})
    assert resp.status_code == 200, resp.text
    added = {a["zernio_account_id"]: a["id"] for a in resp.json()["added"]}
    return added[ACCOUNT_1], added[YOUTUBE]


async def test_each_platform_takes_its_own_texts_from_one_file(client, sample_video):
    await reset_mock()
    tiktok, youtube = await _connect_both(client)
    item = await upload_to_library(client, sample_video, name=REEL)
    patched = await client.patch(f"/api/library/{item['id']}", json={
        "tiktok": {"description": "Para TikTok", "tags": ["#estoicismo"]},
        "youtube": {"title": "Título de YouTube", "description": "Para YouTube",
                    "tags": ["filosofia"], "first_comment": "¿Y tú?"},
    })
    assert patched.status_code == 200

    for account_id in (tiktok, youtube):
        resp = await enqueue(client, await default_queue_id(client, account_id), item["id"])
        assert resp.json()["added"], resp.json()["problems"]
    by_account = {a: (await account_entry(client, a))["videos"][0] for a in (tiktok, youtube)}
    assert by_account[tiktok]["caption"] == "Para TikTok"
    assert by_account[tiktok]["auto_hashtags"] == ["#estoicismo"]
    assert by_account[youtube]["title"] == "Título de YouTube"
    assert by_account[youtube]["caption"] == "Para YouTube"
    assert by_account[youtube]["auto_hashtags"] == ["#filosofia"]
    assert by_account[youtube]["first_comment"] == "¿Y tú?"
    # One file on disk, shared.
    assert by_account[tiktok]["file_path"] == by_account[youtube]["file_path"] == item["file_path"]
    assert all(v["caption_source"] == "biblioteca" for v in by_account.values())
    assert by_account[tiktok]["library_folder"] == SERIES

    usage = (await client.get(f"/api/library/{item['id']}")).json()["queued"]
    assert {u["platform"] for u in usage} == {"tiktok", "youtube"}


async def test_one_account_never_gets_the_same_video_twice(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    night = await add_queue(client, account_id, "Noche")
    item = await upload_to_library(client, sample_video)
    first = await enqueue(client, await default_queue_id(client, account_id), item["id"])
    assert first.json()["added"]
    again = await enqueue(client, night, item["id"])
    assert again.json()["added"] == [] and "ya está en «Cola principal»" in again.json()["problems"][0]

    picker = await _items(client, folder=item["folder"], account_id=account_id)
    assert picker[0]["in_account"] is True
    other = await make_account(client, ACCOUNT_2)
    assert (await _items(client, folder=item["folder"], account_id=other))[0]["in_account"] is False
    # The picker's folder list says how many each account can still take.
    for who, left in ((account_id, 0), (other, 1)):
        (folder,) = (await client.get("/api/library", params={"account_id": who})).json()["folders"]
        assert folder["available"] == left


async def test_a_whole_folder_can_be_queued_at_once(client, sample_video, tmp_root):
    await reset_mock()
    account_id = await make_account(client)
    for i in range(3):
        clip = _variant(sample_video, tmp_root, f"reel_0{i}_{SERIES}_tema-{i}.mp4", bytes([i]) * 8)
        await upload_to_library(client, clip)
    resp = await client.post(f"/api/schedules/{await default_queue_id(client, account_id)}/videos",
                             json={"folder": SERIES})
    assert len(resp.json()["added"]) == 3


async def test_library_edits_reach_the_queues_until_the_queue_takes_its_own(
        client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    night = await add_queue(client, account_id, "Noche")
    item = await upload_to_library(client, sample_video, caption="Primera versión")
    other = _variant(sample_video, sample_video.parent, "otro.mp4", b"\x00")
    second = await upload_to_library(client, other, caption="Otro")
    await enqueue(client, await default_queue_id(client, account_id), item["id"])
    await enqueue(client, night, second["id"])
    videos = {v["original_name"]: v for v in (await account_entry(client, account_id))["videos"]}

    await client.patch(f"/api/library/{item['id']}", json={"tiktok": {"description": "Segunda"}})
    video = next(v for v in (await account_entry(client, account_id))["videos"]
                 if v["id"] == videos[sample_video.name]["id"])
    assert video["caption"] == "Segunda" and video["caption_source"] == "biblioteca"

    # Edited in the queue: its own from now on.
    await client.patch(f"/api/videos/{video['id']}", json={"caption": "Sólo en esta cola"})
    await client.patch(f"/api/library/{item['id']}", json={"tiktok": {"description": "Tercera"}})
    video = next(v for v in (await account_entry(client, account_id))["videos"]
                 if v["id"] == video["id"])
    assert video["caption"] == "Sólo en esta cola" and video["caption_source"] == "manual"


async def test_publishing_leaves_the_library_file_where_it_is(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    item = await upload_to_library(client, sample_video, caption="Hola")
    (video,) = (await enqueue(client, await default_queue_id(client, account_id),
                              item["id"])).json()["added"]
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)
    done = (await account_entry(client, account_id))["videos"][0]
    assert done["status"] == "PUBLISHED", done["last_error"]
    assert done["file_path"] == item["file_path"] and Path(item["file_path"]).exists()
    (post,) = (await mock_state())["posts"].values()
    assert post["content"].startswith("Hola")


async def test_a_youtube_account_publishes_its_title(client, sample_video):
    await reset_mock()
    _, youtube = await _connect_both(client)
    await put_schedule(client, youtube)
    item = await upload_to_library(client, sample_video)
    await client.patch(f"/api/library/{item['id']}", json={
        "youtube": {"title": "El juicio, no el hecho", "description": "Epicteto",
                    "tags": ["#estoicismo"]}})
    (video,) = (await enqueue(client, await default_queue_id(client, youtube),
                              item["id"])).json()["added"]
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(youtube)
    (post,) = (await mock_state())["posts"].values()
    assert post["title"] == "El juicio, no el hecho"
    assert post["platform"] == "youtube"
    assert post["thumbnail_key"] is None


async def test_youtube_and_tiktok_get_the_cover(client, sample_video):
    await reset_mock()
    tiktok, youtube = await _connect_both(client)
    for account_id in (tiktok, youtube):
        await put_schedule(client, account_id)
    res = await _upload(client, (REEL, sample_video.read_bytes(), "video/mp4"),
                        (Path(REEL).stem + "_portada.jpg", COVER, "image/jpeg"))
    (item,) = res["added"]
    for account_id in (tiktok, youtube):
        (video,) = (await enqueue(client, await default_queue_id(client, account_id),
                                  item["id"])).json()["added"]
        await client.post(f"/api/videos/{video['id']}/publish-now")
        await drain(account_id)
    state = await mock_state()
    by_platform = {p["platform"]: p for p in state["posts"].values()}
    for platform in ("tiktok", "youtube"):
        key = by_platform[platform]["thumbnail_key"]
        assert key and key.endswith(".jpg")
        assert state["media"][key]["contentType"] == "image/jpeg"
        assert state["media"][key]["received"] == len(COVER)
    # Zernio's default cover is the frame at 1 s; the cover sits at frame 0.
    assert by_platform["tiktok"]["settings"]["video_cover_timestamp_ms"] == 0
    assert "video_cover_timestamp_ms" not in by_platform["youtube"]["settings"]


async def test_the_library_can_be_filtered_by_queue(client, sample_video, tmp_root):
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    queued = await upload_to_library(client, sample_video, name=REEL)
    loose = await upload_to_library(
        client, _variant(sample_video, tmp_root, "suelto.mp4", b"\0"), name="suelto.mp4")
    await enqueue(client, queue_id, queued["id"])

    ids = lambda rows: [r["id"] for r in rows]  # noqa: E731
    assert ids(await _items(client, queue=str(queue_id))) == [queued["id"]]
    assert ids(await _items(client, queue="any")) == [queued["id"]]
    assert ids(await _items(client, queue="none")) == [loose["id"]]
    assert ids(await _items(client, queue="none", folder=SERIES)) == []
    (row,) = await _items(client, queue=str(queue_id))
    assert row["queued"][0]["queue_name"] and row["queued"][0]["schedule_id"] == queue_id
    assert (await client.get("/api/library/items", params={"queue": "x"})).status_code == 400


async def test_hidden_queues_and_other_accounts_do_not_count_in_the_library(
        client, sample_video, tmp_root):
    """«Cuentas» narrows «en alguna/ninguna cola»; a hidden queue is out of sight."""
    await reset_mock()
    tiktok, youtube = await _connect_both(client)
    on_tiktok = await upload_to_library(client, sample_video, name=REEL)
    on_youtube = await upload_to_library(
        client, _variant(sample_video, tmp_root, "suelto.mp4", b"\0"), name="suelto.mp4")
    tiktok_queue = await default_queue_id(client, tiktok)
    await enqueue(client, tiktok_queue, on_tiktok["id"])
    await enqueue(client, await default_queue_id(client, youtube), on_youtube["id"])

    ids = lambda rows: sorted(r["id"] for r in rows)  # noqa: E731
    both = sorted([on_tiktok["id"], on_youtube["id"]])
    assert ids(await _items(client, queue="any")) == both
    assert ids(await _items(client, queue="any", accounts=[tiktok])) == [on_tiktok["id"]]
    assert ids(await _items(client, queue="none", accounts=[tiktok])) == [on_youtube["id"]]
    assert ids(await _items(client, queue="any", accounts=[tiktok, youtube])) == both

    enabled = lambda: account_entry(client, tiktok)  # noqa: E731
    before = (await enabled())["queues"][0]["schedule"]["enabled"]
    resp = await client.patch(f"/api/schedules/{tiktok_queue}", json={"hidden": True})
    assert resp.status_code == 200 and resp.json()["schedule"]["hidden"] is True
    assert ids(await _items(client, queue="any")) == [on_youtube["id"]]
    assert ids(await _items(client, queue="none")) == [on_tiktok["id"]]
    (row,) = await _items(client, queue="none")
    assert row["queued"][0]["queue_hidden"] is True, "la UI la quita de «📋 En»"
    # Hidden is not stopped: publishing is «enabled», untouched.
    assert (await enabled())["queues"][0]["schedule"]["enabled"] == before

    await client.patch(f"/api/schedules/{tiktok_queue}", json={"hidden": False})
    assert ids(await _items(client, queue="any")) == both


async def test_the_library_can_be_filtered_by_text_status(client, sample_video, tmp_root):
    from app.db import session_scope
    from app.models import LibraryItem

    statuses = {"listo.mp4": "done", "propio.mp4": "none", "cola.mp4": "pending",
                "ahora.mp4": "generating", "roto.mp4": "failed"}
    made = {}
    for i, name in enumerate(statuses):
        made[name] = (await upload_to_library(
            client, _variant(sample_video, tmp_root, name, bytes([i])), name=name))["id"]
    with session_scope() as session:
        for name, status in statuses.items():
            session.get(LibraryItem, made[name]).caption_status = status

    names = lambda rows: sorted(r["original_name"] for r in rows)  # noqa: E731
    assert names(await _items(client, text="written")) == ["listo.mp4", "propio.mp4"]
    assert names(await _items(client, text="writing")) == ["ahora.mp4", "cola.mp4"]
    assert names(await _items(client, text="failed")) == ["roto.mp4"]
    assert (await client.get("/api/library/items", params={"text": "x"})).status_code == 400


# --------------------------------------------------------------------------- #
# Renaming a folder
# --------------------------------------------------------------------------- #

async def _rename(client, old: str, new: str):
    return await client.post("/api/library/folders/rename", json={"old": old, "new": new})


async def test_renaming_a_folder_moves_it_on_disk_and_in_every_row(
        client, sample_video, tmp_root):
    await reset_mock()
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    item = await upload_to_library(client, sample_video, caption="Hola", name=REEL)
    (video,) = (await enqueue(client, await default_queue_id(client, account_id),
                              item["id"])).json()["added"]

    resp = await _rename(client, SERIES, "Estoicos del día")
    assert resp.status_code == 200, resp.text
    assert resp.json()["folder"] == "estoicos-del-dia"
    moved = tmp_root / "library" / "estoicos-del-dia" / REEL
    assert moved.exists() and moved.with_suffix(".txt").read_text(encoding="utf-8") == "Hola"
    assert not (tmp_root / "library" / SERIES).exists()

    row = (await client.get(f"/api/library/{item['id']}")).json()
    assert row["folder"] == "estoicos-del-dia" and Path(row["file_path"]) == moved
    queued = (await account_entry(client, account_id))["videos"][0]
    assert Path(queued["file_path"]) == moved and queued["library_folder"] == "estoicos-del-dia"

    # The queue still publishes it, from where it is now.
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)
    done = (await account_entry(client, account_id))["videos"][0]
    assert done["status"] == "PUBLISHED", done["last_error"]

    # The next video of that series goes to the renamed folder, not the old one.
    other = _variant(sample_video, tmp_root, "otro.mp4", b"\0")
    res = await _upload(client, (REEL.replace("002", "003"), other.read_bytes(), "video/mp4"))
    assert res["added"][0]["folder"] == "estoicos-del-dia"
    assert not (tmp_root / "library" / SERIES).exists()
    # The upload dialog guesses the same from the overview.
    overview = (await client.get("/api/library")).json()
    assert overview["aliases"] == {SERIES: "estoicos-del-dia"}


async def test_renaming_back_and_again_keeps_the_series_pointing_at_the_last_name(
        client, sample_video, tmp_root):
    from app.db import session_scope

    await reset_mock()
    await upload_to_library(client, sample_video, name=REEL)
    assert (await _rename(client, SERIES, "a")).status_code == 200
    assert (await _rename(client, "a", "b")).status_code == 200
    with session_scope() as session:
        assert library.destination(session, REEL) == "b"
    assert (await _rename(client, "b", SERIES)).status_code == 200
    with session_scope() as session:
        assert library.destination(session, REEL) == SERIES
        assert library.aliases(session) == {"a": SERIES, "b": SERIES}
    assert (tmp_root / "library" / SERIES / REEL).exists()


async def test_a_rename_is_refused_when_it_would_clash_or_pull_a_file_being_read(
        client, sample_video, tmp_root):
    await reset_mock()
    item = await upload_to_library(client, sample_video, name=REEL)
    other = _variant(sample_video, tmp_root, "IMG_1.mp4", b"\0")
    await _upload(client, ("IMG_1.mp4", other.read_bytes(), "video/mp4"), folder="Roma")

    assert (await _rename(client, SERIES, "roma")).status_code == 409
    assert (await _rename(client, SERIES, "¡¡!!")).status_code == 409
    assert (await _rename(client, SERIES, SERIES)).status_code == 409
    assert (await _rename(client, "no-existe", "x")).status_code == 404

    with library.reading(item["file_path"]):
        resp = await _rename(client, SERIES, "estoicos")
    assert resp.status_code == 409 and "subiendo o escuchando" in resp.json()["detail"]
    assert (tmp_root / "library" / SERIES / REEL).exists()
    assert (await _rename(client, SERIES, "estoicos")).status_code == 200


async def _move(client, ids, folder):
    return await client.post("/api/library/move", json={"ids": ids, "folder": folder})


async def test_moving_videos_takes_their_files_and_queue_entries_along(
        client, sample_video, tmp_root):
    await reset_mock()
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    item = await upload_to_library(client, sample_video, caption="Hola", name=REEL)
    (tmp_root / "library" / SERIES / REEL).with_name(REEL.replace(".mp4", "_portada.jpg")).write_bytes(b"jpg")
    (video,) = (await enqueue(client, await default_queue_id(client, account_id),
                              item["id"])).json()["added"]
    other = _variant(sample_video, tmp_root, "IMG_1.mp4", b"\0")
    await _upload(client, ("IMG_1.mp4", other.read_bytes(), "video/mp4"), folder="Roma")
    await _label(client, SERIES, ["serie"])

    resp = await _move(client, [item["id"]], "roma")
    assert resp.status_code == 200, resp.text
    assert resp.json()["moved"] == 1 and resp.json()["videos"] == 1
    moved = tmp_root / "library" / "roma" / REEL
    assert moved.exists() and moved.with_suffix(".txt").read_text(encoding="utf-8") == "Hola"
    assert moved.with_name(REEL.replace(".mp4", "_portada.jpg")).exists()
    # The folder left empty is gone, and so are its labels.
    assert not (tmp_root / "library" / SERIES).exists()
    overview = (await client.get("/api/library")).json()
    assert {f["folder"]: f["count"] for f in overview["folders"]} == {"roma": 2}
    assert overview["labels"] == []

    row = (await client.get(f"/api/library/{item['id']}")).json()
    assert row["folder"] == "roma" and Path(row["file_path"]) == moved
    queued = (await account_entry(client, account_id))["videos"][0]
    assert Path(queued["file_path"]) == moved

    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)
    done = (await account_entry(client, account_id))["videos"][0]
    assert done["status"] == "PUBLISHED", done["last_error"]

    # A move is for these videos: the series still uploads to its own folder.
    third = _variant(sample_video, tmp_root, "otro.mp4", b"\1")
    res = await _upload(client, (REEL.replace("002", "003"), third.read_bytes(), "video/mp4"))
    assert res["added"][0]["folder"] == SERIES


async def test_moving_to_a_new_folder_renames_on_clash_and_reports_the_rest(
        client, sample_video, tmp_root):
    await reset_mock()
    first = (await _upload(client, ("clip.mp4", sample_video.read_bytes(), "video/mp4"),
                           folder="a"))["added"][0]
    other = _variant(sample_video, tmp_root, "x.mp4", b"\0")
    second = (await _upload(client, ("clip.mp4", other.read_bytes(), "video/mp4"),
                            folder="b"))["added"][0]

    resp = await _move(client, [first["id"], second["id"]], "Nueva Carpeta")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["folder"] == "nueva-carpeta" and body["moved"] == 2 and not body["problems"]
    names = sorted(p.name for p in (tmp_root / "library" / "nueva-carpeta").iterdir())
    assert names == ["clip-1.mp4", "clip.mp4"]

    again = (await _move(client, [first["id"]], "nueva-carpeta")).json()
    assert again["moved"] == 0 and "ya está" in again["problems"][0]
    assert (await _move(client, [first["id"]], "¡¡!!")).status_code == 422

    with library.reading(second["file_path"]):
        held = (await _move(client, [second["id"]], "a")).json()
    assert held["moved"] == 0 and "subiendo o escuchando" in held["problems"][0]
    # Nothing moved: no empty folder is left behind.
    assert not (tmp_root / "library" / "a").exists()


def test_a_path_read_just_before_a_move_finds_the_file(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "v-1.mp4").write_bytes(b"x")
    old = tmp_path / "a" / "v.mp4"
    library._moved[library._key(old)] = library._key(tmp_path / "b" / "v-1.mp4")
    try:
        with library.reading(old) as current:
            assert current.resolve() == (tmp_path / "b" / "v-1.mp4").resolve()
    finally:
        library._moved.pop(library._key(old), None)


async def _label(client, folder, labels):
    return await client.post("/api/library/folders/labels", json={"folder": folder, "labels": labels})


async def test_folders_carry_labels_that_filter_and_follow_a_rename(client, sample_video, tmp_root):
    await reset_mock()
    await upload_to_library(client, sample_video, name=REEL)
    other = _variant(sample_video, tmp_root, "IMG_1.mp4", b"\0")
    await _upload(client, ("IMG_1.mp4", other.read_bytes(), "video/mp4"), folder="Roma")

    resp = await _label(client, SERIES, ["  Serie ", "#serie", "Lista  para   publicar"])
    assert resp.status_code == 200 and resp.json()["labels"] == ["serie", "lista para publicar"]
    await _label(client, "roma", ["serie"])
    overview = (await client.get("/api/library")).json()
    assert {f["folder"]: f["labels"] for f in overview["folders"]} == {
        SERIES: ["serie", "lista para publicar"], "roma": ["serie"]}
    assert [(r["label"], r["folders"], r["videos"], r["color"]) for r in overview["labels"]] == [
        ("lista para publicar", 1, 1, None), ("serie", 2, 2, None)]
    account_id = await make_account(client)
    by_label = {r["label"]: r for r in (await client.get(
        "/api/library", params={"account_id": account_id})).json()["labels"]}
    assert by_label["serie"]["available"] == 2

    folders = lambda rows: sorted({r["folder"] for r in rows})  # noqa: E731
    assert folders(await _items(client, label="serie")) == sorted(["roma", SERIES])
    assert folders(await _items(client, label="Lista para publicar")) == [SERIES]
    assert await _items(client, label="nada") == []
    assert folders(await _items(client, labels=["serie", "lista para publicar"])) == [SERIES]
    assert folders(await _items(client, labels=["serie", "lista para publicar"], match="any")) == sorted(
        ["roma", SERIES])
    assert folders(await _items(client, folders=["roma"], labels=["serie"])) == ["roma"]

    assert (await _rename(client, SERIES, "estoicos")).status_code == 200
    overview = (await client.get("/api/library")).json()
    assert {f["folder"]: f["labels"] for f in overview["folders"]}["estoicos"] == [
        "serie", "lista para publicar"]

    assert (await _label(client, "roma", [])).json()["labels"] == []
    assert folders(await _items(client, label="serie")) == ["estoicos"]
    assert (await _label(client, "no-existe", ["x"])).status_code == 404
    assert (await _label(client, "roma", ["x" * 40])).status_code == 422


async def test_labels_can_leave_folders_out_and_pick_the_unlabelled_ones(client, sample_video, tmp_root):
    await reset_mock()
    await upload_to_library(client, sample_video, name=REEL)
    for i, folder in enumerate(["Roma", "Atenas"], start=1):
        clip = _variant(sample_video, tmp_root, f"IMG_{i}.mp4", b"\0" * i)
        await _upload(client, (f"IMG_{i}.mp4", clip.read_bytes(), "video/mp4"), folder=folder)
    await _label(client, SERIES, ["serie", "terminada"])
    await _label(client, "roma", ["serie"])

    folders = lambda rows: sorted({r["folder"] for r in rows})  # noqa: E731
    assert folders(await _items(client, exclude=["terminada"])) == ["atenas", "roma"]
    assert folders(await _items(client, labels=["serie"], exclude=["terminada"])) == ["roma"]
    assert folders(await _items(client, exclude=["serie", "terminada"])) == ["atenas"]
    assert folders(await _items(client, unlabeled="include")) == ["atenas"]
    assert folders(await _items(client, unlabeled="exclude")) == sorted(["roma", SERIES])
    # «Sin etiquetas» joins the labels' folders whether they need all or any of them.
    assert folders(await _items(client, labels=["serie", "terminada"], unlabeled="include")) == sorted(
        ["atenas", SERIES])
    assert folders(await _items(client, labels=["terminada"], match="any", unlabeled="include")) == sorted(
        ["atenas", SERIES])
    assert folders(await _items(client, folders=["roma", "atenas"], unlabeled="exclude")) == ["roma"]
    assert (await client.get("/api/library/items", params={"unlabeled": "maybe"})).status_code == 422

    # The search looks inside what the folders and labels keep.
    assert folders(await _items(client, q="IMG", exclude=["serie"])) == ["atenas"]
    assert [r["original_name"] for r in await _items(client, q="img_1", folder="roma")] == ["IMG_1.mp4"]
    assert await _items(client, q="IMG", folder=SERIES) == []


async def test_a_label_keeps_the_colour_picked_for_it(client, sample_video):
    await reset_mock()
    await upload_to_library(client, sample_video, name=REEL)
    await _label(client, SERIES, ["serie"])
    color = lambda label, value: client.post(  # noqa: E731
        "/api/library/labels/color", json={"label": label, "color": value})

    assert (await color(" Serie ", "#AA3366")).json() == {"label": "serie", "color": "#aa3366"}
    (row,) = (await client.get("/api/library")).json()["labels"]
    assert row["color"] == "#aa3366"
    assert (await color("serie", "rojo")).status_code == 422
    assert (await color("serie", None)).json()["color"] is None
    (row,) = (await client.get("/api/library")).json()["labels"]
    assert row["color"] is None


def test_a_path_read_just_before_a_rename_finds_the_file(tmp_path):
    folder = tmp_path / "vieja"
    folder.mkdir()
    (folder / "v.mp4").write_bytes(b"x")
    (tmp_path / "vieja").rename(tmp_path / "nueva")
    library._moved[library._key(folder)] = library._key(tmp_path / "nueva")
    try:
        with library.reading(folder / "v.mp4") as current:
            assert current == (tmp_path / "nueva" / "v.mp4").resolve() or \
                current.resolve() == (tmp_path / "nueva" / "v.mp4").resolve()
            assert current.exists()
    finally:
        library._moved.pop(library._key(folder), None)


# --------------------------------------------------------------------------- #
# Removing
# --------------------------------------------------------------------------- #

async def test_a_video_still_in_a_queue_cannot_leave_the_library(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    item = await upload_to_library(client, sample_video, caption="Hola")
    (video,) = (await enqueue(client, await default_queue_id(client, account_id),
                              item["id"])).json()["added"]
    refused = await client.delete(f"/api/library/{item['id']}")
    assert refused.status_code == 409 and "Cola principal" in refused.json()["detail"]

    # Removing it from the queue never touches the shared file.
    await client.delete(f"/api/videos/{video['id']}?delete_file=true")
    assert Path(item["file_path"]).exists()
    gone = await client.delete(f"/api/library/{item['id']}")
    assert gone.status_code == 200 and gone.json()["file_deleted"] is True
    assert not Path(item["file_path"]).exists()
    assert not Path(item["file_path"]).with_suffix(".txt").exists()
    assert (await client.get("/api/library")).json()["total"] == 0


# --------------------------------------------------------------------------- #
# Writing the texts in the library
# --------------------------------------------------------------------------- #

@pytest.fixture
def ai_on():
    from app.config import get_settings

    s = get_settings()
    saved = (s.caption_ai_enabled, s.ollama_url, s.ollama_model)
    s.caption_ai_enabled, s.ollama_url, s.ollama_model = True, MOCK_BASE, ""
    autocaption.reset_probe_cache()
    yield s
    s.caption_ai_enabled, s.ollama_url, s.ollama_model = saved
    autocaption.reset_probe_cache()


async def test_the_library_writes_both_platforms_once_and_the_queue_waits_for_it(
        client, sample_video, ai_on, monkeypatch):
    await reset_mock()
    calls = []

    def transcribe(path, settings, *, max_seconds=None):
        calls.append(path)
        return autocaption.Transcript(text=SHEET.split("\n")[0], language="es", duration=20.0)

    monkeypatch.setattr(autocaption, "transcribe", transcribe)
    tiktok, youtube = await _connect_both(client)
    res = await _upload(client, (REEL, sample_video.read_bytes(), "video/mp4"),
                        (Path(REEL).with_suffix(".txt").name, SHEET.encode(), "text/plain"), ai=True)
    (item,) = res["added"]
    assert item["caption_status"] == "pending"
    for account_id in (tiktok, youtube):
        (video,) = (await enqueue(client, await default_queue_id(client, account_id),
                                  item["id"])).json()["added"]
        assert video["caption_status"] == "pending" and video["caption_source"] == "biblioteca"
    # The queues wait for the library; they are not what the captioner writes.
    assert (await client.get("/api/state")).json()["captions_pending"] == 0

    from app.scheduler.captioner import captioner
    assert await captioner.run_once() is True
    assert await captioner.run_once() is False, "una sola pasada por vídeo, no una por cola"

    (row,) = await _items(client, folder=SERIES)
    assert row["caption_status"] == "done" and row["caption_source"] == "ficha+ollama"
    for platform in ("tiktok", "youtube"):
        assert row["texts"][platform]["title"] == "Cinco hábitos para rendir más cada día"
        assert row["texts"][platform]["description"].startswith("Cinco hábitos que sí cambian")
        assert row["texts"][platform]["tags"][:1] == ["#habitos"]
    for account_id in (tiktok, youtube):
        video = (await account_entry(client, account_id))["videos"][0]
        assert video["caption_status"] == "done" and video["caption_source"] == "biblioteca"
        assert video["caption"] == row["texts"]["tiktok"]["description"]
    assert (await mock_state())["ollama_calls"][0]["format"]["required"][0] == "titulo"
    events = (await client.get("/api/events?kind=library_caption_generated")).json()["events"]
    assert events and "llegan a 2 vídeo(s)" in events[0]["message"]


async def test_rewrite_for_this_queue_writes_its_own(client, sample_video, ai_on, monkeypatch):
    await reset_mock()
    monkeypatch.setattr(autocaption, "transcribe", lambda path, settings, max_seconds=None:
                        autocaption.Transcript(text="Hoy hablamos de hábitos y de rutina diaria "
                                                    "para rendir más.", language="es"))
    account_id = await make_account(client)
    item = await upload_to_library(client, sample_video, caption="Texto de la biblioteca")
    (video,) = (await enqueue(client, await default_queue_id(client, account_id), item["id"],
                              rewrite=True)).json()["added"]
    assert video["caption_status"] == "pending" and video["caption_source"] is None
    assert (await client.get("/api/state")).json()["captions_pending"] == 1

    from app.scheduler.captioner import captioner
    assert await captioner.run_once() is True
    video = (await account_entry(client, account_id))["videos"][0]
    assert video["caption_source"] == "whisper+ollama"
    # The library keeps its own text.
    assert (await _items(client, folder=item["folder"]))[0]["texts"]["tiktok"]["description"] \
        == "Texto de la biblioteca"


# --------------------------------------------------------------------------- #
# Moving the media from before the library: copy, then finalize
# --------------------------------------------------------------------------- #

def test_a_copy_is_byte_identical_and_never_left_half_done(sample_video, tmp_root):
    from app.services import library_migration as migration

    target = tmp_root / "library" / "x" / "copia.mp4"
    how = migration.clone_or_copy(sample_video, target)
    assert how in ("clone", "copy")
    assert migration.same_bytes(sample_video, target)
    assert not list(target.parent.glob(".*.part"))


async def test_old_media_is_copied_once_with_its_texts_and_then_finalized(
        client, sample_video, tmp_root):
    from app.config import get_settings
    from app.db import session_scope
    from app.models import VideoItem, VideoStatus
    from app.services import library_migration as migration

    await reset_mock()
    tiktok, youtube = await _connect_both(client)
    inbox, posted = tmp_root / "inbox", tmp_root / "posted"
    stem = f"reel_002_{SERIES}_el-juicio-no-el-hecho"
    # The same reel twice: published on TikTok (moved to posted/), waiting for
    # YouTube in the inbox under the "-1" the second upload got.
    for folder, name in ((posted, f"{stem}.mp4"), (inbox, f"{stem}-1.mp4")):
        (folder / name).write_bytes(sample_video.read_bytes())
        (folder / name).with_suffix(".txt").write_text(SHEET, encoding="utf-8")
    loose = inbox / "reel_05_otra-serie_tema.mp4"  # nobody queued it
    loose.write_bytes(sample_video.read_bytes() + b"\x05")
    orphan_sheet = posted / "reel_29_experimentos_sin-video.txt"
    orphan_sheet.write_text(SHEET, encoding="utf-8")

    from app.services import media
    digest = media.checksum(posted / f"{stem}.mp4")
    with session_scope() as session:
        session.add_all([
            VideoItem(account_id=tiktok, schedule_id=await default_queue_id(client, tiktok),
                      file_path=str(posted / f"{stem}.mp4"), original_name=f"{stem}.mp4",
                      checksum=digest, caption="Texto de TikTok", auto_hashtags=["#estoicismo"],
                      caption_status="done", caption_source="ficha+ollama",
                      status=VideoStatus.PUBLISHED),
            VideoItem(account_id=youtube, schedule_id=await default_queue_id(client, youtube),
                      file_path=str(inbox / f"{stem}-1.mp4"), original_name=f"{stem}-1.mp4",
                      checksum=digest, title="Título de YouTube", caption="Texto de YouTube",
                      first_comment="¿Y tú?", auto_hashtags=["#filosofia"],
                      caption_status="done", caption_source="ficha+ollama",
                      status=VideoStatus.DELEGATED, zernio_post_id="zp_1"),
        ])
    settings = get_settings()

    with session_scope() as session:
        dry = migration.copy_all(session, settings, dry_run=True)
        session.rollback()
    assert dry.groups == 2 and dry.copied == 2 and dry.bytes_saved > 0
    assert not list((tmp_root / "library").rglob("*.mp4")), "el ensayo no copia nada"

    with session_scope() as session:
        report = migration.copy_all(session, settings, dry_run=False)
    assert report.copied == 2 and report.registered == 2 and report.linked_rows == 2
    shared = tmp_root / "library" / SERIES / f"{stem}.mp4"
    assert shared.exists() and shared.with_suffix(".txt").exists()
    assert (tmp_root / "library" / "otra-serie" / loose.name).exists()
    assert (tmp_root / "library" / "experimentos" / orphan_sheet.name).exists()
    # Nothing moved: the running app still reads the same paths.
    assert (posted / f"{stem}.mp4").exists() and (inbox / f"{stem}-1.mp4").exists()

    (row,) = await _items(client, folder=SERIES)
    assert row["texts"]["tiktok"]["description"] == "Texto de TikTok"
    assert row["texts"]["youtube"]["description"] == "Texto de YouTube"
    # TikTok's row had no title or pinned comment: they come from YouTube's.
    assert row["texts"]["tiktok"]["title"] == row["texts"]["youtube"]["title"] == "Título de YouTube"
    assert row["texts"]["tiktok"]["first_comment"] == "¿Y tú?"
    assert row["caption_status"] == "done" and len(row["queued"]) == 2
    videos = [v for a in (tiktok, youtube) for v in (await account_entry(client, a))["videos"]]
    assert {v["file_path"] for v in videos} == {str(posted / f"{stem}.mp4"),
                                                str(inbox / f"{stem}-1.mp4")}
    assert all(v["library_item_id"] == row["id"] for v in videos)

    with session_scope() as session:
        again = migration.copy_all(session, settings, dry_run=False)
    assert again.copied == 0 and again.already == 2 and again.registered == 0

    # A library copy that is not identical keeps its original where it is.
    tampered = tmp_root / "library" / "otra-serie" / loose.name
    tampered.write_bytes(tampered.read_bytes() + b"!")
    with session_scope() as session:
        check = migration.finalize(session, settings, apply=False)
        session.rollback()
    assert check.verified == 2 and (posted / f"{stem}.mp4").exists()

    with session_scope() as session:
        done = migration.finalize(session, settings, apply=True)
    assert done.deleted == 2 and done.rows_repointed == 2
    assert not (posted / f"{stem}.mp4").exists() and not (inbox / f"{stem}-1.mp4").exists()
    assert not (posted / f"{stem}.txt").exists() and not orphan_sheet.exists()
    assert loose.exists() and any(loose.name in line for line in done.kept)
    videos = [v for a in (tiktok, youtube) for v in (await account_entry(client, a))["videos"]]
    assert {v["file_path"] for v in videos} == {str(shared)}


async def test_any_text_edited_in_the_queue_stops_following_the_library(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    item = await upload_to_library(client, sample_video, caption="Biblioteca")
    (video,) = (await enqueue(client, await default_queue_id(client, account_id),
                              item["id"])).json()["added"]
    await client.patch(f"/api/videos/{video['id']}", json={"hashtags": ["#mio"]})
    await client.patch(f"/api/library/{item['id']}", json={"tiktok": {"tags": ["#otro"],
                                                                      "description": "Nuevo"}})
    video = (await account_entry(client, account_id))["videos"][0]
    assert video["auto_hashtags"] == ["#mio"] and video["caption"] == "Biblioteca"
    assert video["caption_source"] == "manual"
