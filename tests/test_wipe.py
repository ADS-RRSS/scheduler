"""«Borrar todo»: three consecutive challenges, then an empty database and disk.

The button is irreversible from inside the app, so what is tested here is as
much what it refuses to do as what it does: no wipe without the three answers,
nothing deleted outside the app's own media folders, and a backup on the way
out.
"""
from __future__ import annotations

from pathlib import Path

from tests.helpers import (
    account_entry,
    default_queue_id,
    make_account,
    put_queue_schedule,
    reset_mock,
    upload_to_queue,
)


async def _answer(client, challenge: dict) -> dict:
    """Solve one challenge the way the dialog does."""
    left, right = challenge["question"].split("+")
    resp = await client.post("/api/admin/wipe/answer", json={
        "token": challenge["token"],
        "total": int(left) + int(right),
        "word": challenge["word"],
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _solve_all(client) -> str:
    """Run the whole gauntlet and return a token cleared to wipe."""
    challenge = (await client.post("/api/admin/wipe/challenge")).json()
    for _ in range(3):
        result = await _answer(client, challenge)
        if result.get("ready"):
            return result["token"]
        challenge = result
    raise AssertionError("las tres comprobaciones no dieron paso al borrado")


async def _seed(client, sample_video: Path, count: int = 2) -> int:
    await reset_mock()
    account_id = await make_account(client)
    queue_id = await default_queue_id(client, account_id)
    await put_queue_schedule(client, queue_id, max_per_day=None)
    for i in range(count):
        copy = sample_video.with_name(f"wipe-{i}-{sample_video.name}")
        copy.write_bytes(sample_video.read_bytes() + bytes([i]))
        await upload_to_queue(client, queue_id, copy)
        copy.unlink(missing_ok=True)
    return account_id


async def test_preview_counts_what_would_go(client, sample_video):
    await _seed(client, sample_video, 2)
    preview = (await client.get("/api/admin/wipe/preview")).json()
    assert preview["tables"]["video_items"] == 2
    assert preview["tables"]["accounts"] == 1
    assert preview["rows"] >= 4
    assert preview["files"] == 2 and preview["bytes"] > 0
    assert preview["delegated"] == 0 and preview["in_flight"] == 0
    # The library and the three folders from before it.
    assert len(preview["folders"]) == 4


async def test_a_wrong_answer_cancels_the_whole_thing(client, sample_video):
    account_id = await _seed(client, sample_video, 1)
    challenge = (await client.post("/api/admin/wipe/challenge")).json()
    assert challenge["step"] == 1 and challenge["steps"] == 3

    bad = await client.post("/api/admin/wipe/answer", json={
        "token": challenge["token"], "total": 999, "word": challenge["word"],
    })
    assert bad.status_code == 400
    assert "cancela" in bad.json()["detail"]

    # The token is dead: there is no second chance on the same run.
    again = await client.post("/api/admin/wipe/answer", json={
        "token": challenge["token"], "total": 2, "word": challenge["word"],
    })
    assert again.status_code == 409
    wipe = await client.post("/api/admin/wipe", json={"token": challenge["token"]})
    assert wipe.status_code == 409

    entry = await account_entry(client, account_id)
    assert len(entry["videos"]) == 1, "no se ha borrado nada"


async def test_the_wrong_word_counts_as_a_wrong_answer(client, sample_video):
    await _seed(client, sample_video, 1)
    challenge = (await client.post("/api/admin/wipe/challenge")).json()
    left, right = challenge["question"].split("+")
    bad = await client.post("/api/admin/wipe/answer", json={
        "token": challenge["token"], "total": int(left) + int(right), "word": "otra",
    })
    assert bad.status_code == 400


async def test_three_answers_are_needed_before_wiping(client, sample_video):
    await _seed(client, sample_video, 1)
    challenge = (await client.post("/api/admin/wipe/challenge")).json()
    second = await _answer(client, challenge)
    assert second["step"] == 2 and second["ready"] is False

    early = await client.post("/api/admin/wipe", json={"token": second["token"]})
    assert early.status_code == 409
    assert "confirmaciones" in early.json()["detail"]

    third = await _answer(client, second)
    assert third["step"] == 3 and third["ready"] is False
    assert (await _answer(client, third))["ready"] is True


async def test_wiping_empties_the_database_and_frees_the_disk(client, sample_video,
                                                              tmp_root):
    account_id = await _seed(client, sample_video, 3)
    library = tmp_root / "library"
    assert len(list(library.rglob("*.mp4"))) == 3
    # A video from before the library goes too.
    inbox = tmp_root / "inbox"
    (inbox / "viejo.mp4").write_bytes(b"old")
    # Two files that must survive: one outside the app's folders, one inside
    # but not a video.
    outsider = tmp_root / "no-lo-toques.mp4"
    outsider.write_bytes(b"mine")
    bystander = inbox / "notas.md"
    bystander.write_text("apuntes")

    token = await _solve_all(client)
    resp = await client.post("/api/admin/wipe", json={"token": token})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["files_deleted"] == 4
    assert body["bytes_freed"] > 0
    assert body["backup"]["name"].startswith("scheduler-")
    assert body["delegated_left_in_zernio"] == 0

    assert list(inbox.glob("*.mp4")) == [] and list(library.rglob("*.mp4")) == []
    assert list(library.iterdir()) == [], "las carpetas de serie vacías se van"
    assert outsider.exists(), "nada fuera de media/ se toca"
    assert bystander.exists(), "sólo se borran vídeos"

    # The database is empty but usable, and the app still answers.
    state = (await client.get("/api/state")).json()
    assert state["accounts"] == []
    tables = {t["name"]: t["rows"] for t in (await client.get("/api/db/tables")).json()["tables"]}
    assert tables["video_items"] == 0 and tables["accounts"] == 0
    # The wipe leaves its own trace, and the schema version survives it.
    assert tables["events"] == 1
    system = (await client.get("/api/admin/system")).json()
    assert system["schema_version"] and system["accounts"] == 0
    assert account_id, "la cuenta existía antes del borrado"

    backups = (await client.get("/api/admin/backups")).json()["backups"]
    assert any(b["name"] == body["backup"]["name"] for b in backups)


async def test_the_token_cannot_be_used_twice(client, sample_video):
    await _seed(client, sample_video, 1)
    token = await _solve_all(client)
    assert (await client.post("/api/admin/wipe", json={"token": token})).status_code == 200
    again = await client.post("/api/admin/wipe", json={"token": token})
    assert again.status_code == 409


async def test_the_preview_warns_about_videos_left_in_zernio(client, sample_video):
    account_id = await _seed(client, sample_video, 2)
    queue_id = await default_queue_id(client, account_id)
    await client.post(f"/api/schedules/{queue_id}/delegate", json={})

    preview = (await client.get("/api/admin/wipe/preview")).json()
    assert preview["delegated"] == 2, "Zernio los publicará aunque se borre la base"
