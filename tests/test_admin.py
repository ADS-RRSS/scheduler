"""Admin surface: account management, activity filters, backups, maintenance
and the database browser."""
from __future__ import annotations

import csv
import io
from pathlib import Path

from app.db import SCHEMA_VERSION
from tests.helpers import (
    ACCOUNT_1,
    ACCOUNT_2,
    ZERNIO_KEY,
    configure_mock,
    default_queue_id,
    drain,
    make_account,
    mock_state,
    put_schedule,
    reset_mock,
    upload_sample,
    upload_to_queue,
)


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #

async def test_account_can_be_renamed_and_toggled(client):
    await reset_mock()
    account_id = await make_account(client)
    resp = await client.patch(f"/api/accounts/{account_id}",
                              json={"display_name": "Marca", "is_active": False})
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Marca" and resp.json()["is_active"] is False
    events = (await client.get("/api/events", params={"kind": "account_toggled"})).json()["events"]
    assert len(events) == 1 and "desactivada" in events[0]["message"]


async def test_stored_key_can_be_revealed_and_the_access_is_logged(client):
    await reset_mock()
    account_id = await make_account(client)
    resp = await client.get(f"/api/accounts/{account_id}/key")
    assert resp.status_code == 200 and resp.json()["api_key"] == ZERNIO_KEY
    events = (await client.get("/api/events", params={"kind": "account_key_revealed"})).json()["events"]
    assert len(events) == 1


async def test_key_replacement_is_validated_against_zernio(client):
    await reset_mock()
    account_id = await make_account(client)

    bad = await client.put(f"/api/accounts/{account_id}/key", json={"api_key": "wrong-key-1234"})
    assert bad.status_code == 400 and "API key" in bad.json()["detail"]

    # A valid key that no longer covers this TikTok account is refused too.
    await configure_mock(key="new-key-5678", hide_accounts=[ACCOUNT_1])
    refused = await client.put(f"/api/accounts/{account_id}/key", json={"api_key": "new-key-5678"})
    assert refused.status_code == 400 and "no da acceso" in refused.json()["detail"]

    await configure_mock(hide_accounts=[])
    ok = await client.put(f"/api/accounts/{account_id}/key", json={"api_key": "new-key-5678"})
    assert ok.status_code == 200
    assert (await client.get(f"/api/accounts/{account_id}/key")).json()["api_key"] == "new-key-5678"


async def test_connection_test_reports_missing_accounts(client):
    await reset_mock()
    account_id = await make_account(client)
    ok = (await client.post(f"/api/accounts/{account_id}/test")).json()
    assert ok["ok"] is True and ok["account"]["username"] == "relay_user"
    assert ok["account_json"]["last_checked_at"] is not None

    await configure_mock(hide_accounts=[ACCOUNT_1])
    gone = (await client.post(f"/api/accounts/{account_id}/test")).json()
    assert gone["ok"] is False and "ya no aparece" in gone["message"]

    await configure_mock(key="other")
    assert (await client.post(f"/api/accounts/{account_id}/test")).status_code == 400


async def test_account_deletion_removes_its_queue(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    await upload_sample(client, account_id, sample_video)
    assert (await client.delete(f"/api/accounts/{account_id}")).status_code == 200
    assert (await client.get("/api/state")).json()["accounts"] == []
    rows = (await client.get("/api/db/tables/video_items")).json()
    assert rows["total"] == 0


async def test_video_can_be_moved_between_accounts(client, sample_video):
    await reset_mock()
    first = await make_account(client, ACCOUNT_1)
    second = await make_account(client, ACCOUNT_2)
    await put_schedule(client, first, enabled=False)
    target_queue = await default_queue_id(client, second)
    video = await upload_sample(client, first, sample_video)

    moved = await client.patch(f"/api/videos/{video['id']}", json={"schedule_id": target_queue})
    assert moved.status_code == 200
    assert moved.json()["account_id"] == second
    assert moved.json()["schedule_id"] == target_queue
    accounts = (await client.get("/api/state")).json()["accounts"]
    counts = {a["account"]["id"]: len(a["videos"]) for a in accounts}
    assert counts == {first: 0, second: 1}

    # Moving a duplicate into an account that already has the same file is refused.
    again = await upload_sample(client, first, sample_video)
    dup = await client.patch(f"/api/videos/{again['id']}", json={"schedule_id": target_queue})
    assert dup.status_code == 409


# --------------------------------------------------------------------------- #
# Activity, maintenance, backups, system
# --------------------------------------------------------------------------- #

async def test_events_can_be_filtered(client):
    await reset_mock()
    account_id = await make_account(client, label="Filtrada")
    await client.post("/api/system/pause", json={"paused": True})
    warn = (await client.get("/api/events", params={"level": "warn"})).json()["events"]
    assert warn and all(e["level"] == "warn" for e in warn)
    mine = (await client.get("/api/events", params={"account_id": account_id})).json()["events"]
    assert mine and all(e["account_id"] == account_id for e in mine)
    found = (await client.get("/api/events", params={"q": "Filtrada"})).json()["events"]
    assert len(found) == 1
    await client.post("/api/system/pause", json={"paused": False})


async def test_events_can_be_pruned(client):
    await reset_mock()
    await make_account(client)
    for _ in range(3):
        await client.post("/api/system/pause", json={"paused": False})
    resp = await client.post("/api/admin/events/prune", json={"keep": 1})
    assert resp.status_code == 200 and resp.json()["removed"] >= 3
    # Only the kept one plus the prune's own log line remain.
    assert len((await client.get("/api/events")).json()["events"]) == 2


async def test_failed_videos_can_be_cleared(client, sample_video):
    await reset_mock()
    account_id = await make_account(client)
    video = await upload_sample(client, account_id, sample_video)
    await configure_mock(key="other")  # the key stops working: fails for good
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)
    assert (await client.get("/api/state")).json()["accounts"][0]["stats"]["failed"] == 1
    resp = await client.post("/api/admin/videos/clear-failed",
                             json={"account_id": account_id, "delete_files": True})
    assert resp.json()["removed"] == 1
    assert (await client.get("/api/state")).json()["accounts"][0]["videos"] == []
    # The file is the library's: it stays for the other queues.
    assert Path(video["file_path"]).exists()


async def test_backups_are_created_listed_downloaded_and_deleted(client):
    await reset_mock()
    await make_account(client)
    created = await client.post("/api/admin/backups")
    assert created.status_code == 200, created.text
    name = created.json()["name"]
    assert name.startswith("scheduler-") and name.endswith(".db")

    listed = (await client.get("/api/admin/backups")).json()["backups"]
    assert [b["name"] for b in listed] == [name]

    download = await client.get(f"/api/admin/backups/{name}")
    assert download.status_code == 200
    assert download.content.startswith(b"SQLite format 3")

    assert (await client.get("/api/admin/backups/../../etc/passwd")).status_code in (400, 404)
    assert (await client.delete(f"/api/admin/backups/{name}")).status_code == 200
    assert (await client.get("/api/admin/backups")).json()["backups"] == []


async def test_system_info_is_reported(client):
    await reset_mock()
    await make_account(client)
    info = (await client.get("/api/admin/system")).json()
    assert info["version"] and info["sqlite"] and info["schema_version"] == SCHEMA_VERSION
    assert info["accounts"] == 1
    assert set(info["media"]) == {"library", "inbox", "posted", "failed"}
    assert "QUEUED" in info["videos"]
    assert info["zernio_api_base"].endswith("/v1")


# --------------------------------------------------------------------------- #
# Database browser
# --------------------------------------------------------------------------- #

async def test_tables_are_listed_with_counts(client):
    await reset_mock()
    await make_account(client)
    tables = {t["name"]: t for t in (await client.get("/api/db/tables")).json()["tables"]}
    assert {"accounts", "schedules", "video_items", "events", "settings",
            "video_metrics", "comments"} <= set(tables)
    assert tables["accounts"]["rows"] == 1 and tables["accounts"]["columns"] > 5


async def test_table_rows_are_paginated_sorted_searched_and_masked(client, sample_video, tmp_root):
    await reset_mock()
    account_id = await make_account(client, label="Principal")
    queue_id = await default_queue_id(client, account_id)
    for i in range(3):
        clip = tmp_root / f"clip{i}.mp4"
        clip.write_bytes(sample_video.read_bytes() + bytes([i]))
        await upload_to_queue(client, queue_id, clip)

    page = (await client.get("/api/db/tables/video_items",
                             params={"per_page": 2, "page": 1, "sort": "id", "dir": "asc"})).json()
    assert page["total"] == 3 and page["pages"] == 2 and len(page["rows"]) == 2
    assert [r["original_name"] for r in page["rows"]] == ["clip0.mp4", "clip1.mp4"]
    assert page["pk"] == "id"

    found = (await client.get("/api/db/tables/video_items", params={"q": "clip2"})).json()
    assert found["total"] == 1 and found["rows"][0]["original_name"] == "clip2.mp4"

    accounts = (await client.get("/api/db/tables/accounts")).json()
    key_column = next(c for c in accounts["columns"] if c["name"] == "api_key_enc")
    assert key_column["secret"] is True
    assert accounts["rows"][0]["api_key_enc"] == "••••••••"
    assert ZERNIO_KEY not in accounts["rows"][0].values()

    assert (await client.get("/api/db/tables/nope")).status_code == 404
    assert (await client.get("/api/db/tables/accounts", params={"sort": "nope"})).status_code == 400


async def test_rows_can_be_read_edited_and_deleted(client):
    await reset_mock()
    account_id = await make_account(client)

    row = (await client.get(f"/api/db/tables/accounts/rows/{account_id}")).json()
    assert row["row"]["display_name"] == "Relay User"

    updated = await client.put(f"/api/db/tables/accounts/rows/{account_id}",
                               json={"values": {"display_name": "Editada", "is_active": "false"}})
    assert updated.status_code == 200, updated.text
    assert updated.json()["row"]["display_name"] == "Editada"
    assert updated.json()["row"]["is_active"] == 0
    state = (await client.get("/api/state")).json()["accounts"][0]["account"]
    assert state["display_name"] == "Editada" and state["is_active"] is False

    assert (await client.put(f"/api/db/tables/accounts/rows/{account_id}",
                             json={"values": {"api_key_enc": "x"}})).status_code == 400
    assert (await client.put(f"/api/db/tables/accounts/rows/{account_id}",
                             json={"values": {"id": 99}})).status_code == 400
    assert (await client.put(f"/api/db/tables/accounts/rows/{account_id}",
                             json={"values": {"is_active": "quizás"}})).status_code == 400
    assert (await client.put("/api/db/tables/accounts/rows/999",
                             json={"values": {"display_name": "x"}})).status_code == 404

    events = (await client.get("/api/events", params={"kind": "db_row_updated"})).json()["events"]
    assert len(events) == 1

    # Delete an older row (not the newest: SQLite would hand its id to the
    # event that logs the deletion itself).
    oldest = (await client.get("/api/events", params={"kind": "account_connected"})).json()["events"][0]
    assert (await client.delete(f"/api/db/tables/events/rows/{oldest['id']}")).status_code == 200
    assert (await client.get(f"/api/db/tables/events/rows/{oldest['id']}")).status_code == 404
    assert (await client.delete(f"/api/db/tables/events/rows/{oldest['id']}")).status_code == 404


async def test_sql_console_is_read_only(client):
    await reset_mock()
    await make_account(client)
    ok = await client.post("/api/db/query", json={"sql": "SELECT id, display_name FROM accounts"})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["columns"] == ["id", "display_name"] and body["count"] == 1

    masked = (await client.post("/api/db/query", json={"sql": "select api_key_enc from accounts"})).json()
    assert masked["rows"][0][0] == "••••••••"

    pragma = await client.post("/api/db/query", json={"sql": "PRAGMA table_info(accounts)"})
    assert pragma.status_code == 200 and pragma.json()["count"] > 5

    for bad in ("UPDATE accounts SET display_name='x'", "DELETE FROM accounts",
                "SELECT 1; DROP TABLE accounts", "PRAGMA journal_mode = DELETE",
                "-- comentario\nINSERT INTO settings VALUES ('a', '{}', '2026-01-01')"):
        resp = await client.post("/api/db/query", json={"sql": bad})
        assert resp.status_code == 400, bad

    broken = await client.post("/api/db/query", json={"sql": "SELECT * FROM no_existe"})
    assert broken.status_code == 400 and "SQLite" in broken.json()["detail"]

    for _ in range(2):
        await client.post("/api/system/pause", json={"paused": False})
    limited = (await client.post("/api/db/query",
                                 json={"sql": "SELECT * FROM events", "limit": 1})).json()
    assert limited["count"] == 1 and limited["truncated"] is True
    # Nothing was written.
    assert (await client.get("/api/state")).json()["accounts"][0]["account"]["display_name"] == "Relay User"


async def test_table_export_is_csv_with_masked_secrets(client):
    await reset_mock()
    await make_account(client)
    resp = await client.get("/api/db/tables/accounts/export.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(resp.text)))
    assert rows[0][0] == "id" and len(rows) == 2
    assert "api_key_enc" in rows[0]
    assert rows[1][rows[0].index("api_key_enc")] == "••••••••"
