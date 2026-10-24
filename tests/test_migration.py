"""Existing installations must survive schema changes without losing data."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text

from app.config import get_settings
from app.crypto import decrypt, encrypt
from app.db import SCHEMA_VERSION, add_missing_columns, engine, init_db, migrate, session_scope
from app.models import Account, LibraryItem, Schedule, VideoItem, VideoStatus


def test_v11_takes_every_path_out_of_the_old_project_folder(fresh_db, tmp_path):
    """Through the old link or not, existing or not: only the old folder's paths change."""
    settings = get_settings()
    old_library = tmp_path / "TikTokScheduler" / "media" / "library"
    library = Path(settings.media_library_dir)
    posted = Path(settings.media_posted_dir)
    paths = {
        "enlazado.mp4": (old_library / "serie" / "enlazado.mp4",
                         library / "serie" / "enlazado.mp4"),
        "publicado.mp4": (old_library.parent / "posted" / "publicado.mp4",
                          posted / "publicado.mp4"),
        "ya-nuevo.mp4": (library / "serie" / "ya-nuevo.mp4", library / "serie" / "ya-nuevo.mp4"),
        "fuera.mp4": (tmp_path / "TikTokScheduler" / "fuera.mp4",
                      tmp_path / "TikTokScheduler" / "fuera.mp4"),
        "otra-cosa.mp4": (old_library.parent / "otra" / "otra-cosa.mp4",
                          old_library.parent / "otra" / "otra-cosa.mp4"),
    }
    with session_scope() as session:
        account = Account(external_id="zernio:acc1", zernio_account_id="acc1",
                          username="creator", display_name="creator")
        session.add(account)
        session.flush()
        queue = Schedule(account_id=account.id, name="Cola principal")
        session.add(queue)
        session.flush()
        for i, (name, (old, _)) in enumerate(paths.items()):
            session.add(LibraryItem(folder="serie", file_path=str(old), original_name=name,
                                    checksum=f"sum-{i}", size_bytes=3))
            session.add(VideoItem(account_id=account.id, schedule_id=queue.id,
                                  file_path=str(old), original_name=name,
                                  checksum=f"sum-{i}", size_bytes=3,
                                  status=VideoStatus.PUBLISHED))
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE settings SET value = '{\"value\": 10}' WHERE key = 'schema_version'"))

    saved = settings.legacy_library_dirs
    settings.legacy_library_dirs = [old_library]
    try:
        actions = migrate()
    finally:
        settings.legacy_library_dirs = saved
    assert any("4 file path(s)" in a for a in actions), actions

    with session_scope() as session:
        for model in (LibraryItem, VideoItem):
            by_name = {r.original_name: r.file_path for r in session.query(model).all()}
            assert by_name == {name: str(new) for name, (_, new) in paths.items()}

    assert migrate() == []


def _columns(table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _tables() -> set[str]:
    return set(inspect(engine).get_table_names())


def test_missing_columns_are_added_with_their_defaults(fresh_db):
    with session_scope() as session:
        account = Account(external_id="zernio:m", zernio_account_id="m", username="m")
        session.add(account)
        session.flush()
        session.add(Schedule(account_id=account.id))

    # Simulate a database created by an older version.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE schedules DROP COLUMN is_aigc"))
        conn.execute(text("ALTER TABLE accounts DROP COLUMN profile_name"))
    assert "is_aigc" not in _columns("schedules")

    added = add_missing_columns()
    assert set(added) == {"schedules.is_aigc added", "accounts.profile_name added"}

    with session_scope() as session:
        schedule = session.query(Schedule).one()
        assert schedule.is_aigc is True
        assert session.query(Account).one().profile_name is None


def test_init_db_is_idempotent(fresh_db):
    assert init_db() == []
    assert migrate() == []


def _build_legacy_database(conn) -> None:
    """Turn the freshly created v2 schema back into the v1 layout, with data
    shaped like a real pre-cleanup installation: a local (manual) account and
    a Zernio account sharing most of their queue."""
    # v1 column names and the columns v2 no longer has.
    conn.execute(text("ALTER TABLE accounts RENAME COLUMN external_id TO open_id"))
    # v1 stored the Zernio id in a *nullable* column (local/OAuth accounts had none).
    conn.execute(text("ALTER TABLE accounts DROP COLUMN zernio_account_id"))
    conn.execute(text("ALTER TABLE accounts ADD COLUMN relay_account_id VARCHAR(128)"))
    conn.execute(text("ALTER TABLE accounts RENAME COLUMN api_key_enc TO relay_api_key_enc"))
    for column, kind in (("union_id", "VARCHAR(128)"), ("avatar_url", "TEXT"),
                         ("access_token_enc", "TEXT"), ("refresh_token_enc", "TEXT"),
                         ("access_expires_at", "DATETIME"), ("refresh_expires_at", "DATETIME"),
                         ("scopes", "TEXT"), ("creator_info", "JSON"), ("creator_info_at", "DATETIME"),
                         ("relay_provider", "VARCHAR(32)"), ("relay_url", "TEXT")):
        conn.execute(text(f'ALTER TABLE accounts ADD COLUMN "{column}" {kind}'))
    conn.execute(text("ALTER TABLE accounts DROP COLUMN profile_name"))
    conn.execute(text("ALTER TABLE accounts DROP COLUMN last_checked_at"))
    # v3 columns: an account had no limits of its own, and exactly one queue.
    conn.execute(text("ALTER TABLE accounts DROP COLUMN max_per_day"))
    conn.execute(text("ALTER TABLE accounts DROP COLUMN min_gap_minutes"))

    conn.execute(text("ALTER TABLE schedules ADD COLUMN post_mode VARCHAR(6) NOT NULL DEFAULT 'DIRECT'"))
    conn.execute(text("ALTER TABLE schedules ADD COLUMN video_cover_timestamp_ms INTEGER DEFAULT 1000"))
    # Columns v4 and v6 removed; a v1 database still has them.
    _add_pre_v4_columns(conn)
    _add_pre_v6_columns(conn)
    _add_pre_v8_columns(conn)
    conn.execute(text("ALTER TABLE schedules DROP COLUMN name"))
    conn.execute(text("ALTER TABLE schedules DROP COLUMN position"))
    conn.execute(text("DROP INDEX IF EXISTS ix_schedules_account_id"))
    conn.execute(text("CREATE UNIQUE INDEX ix_schedules_account_id ON schedules (account_id)"))

    # video_items is rebuilt rather than altered: SQLite refuses to DROP a
    # column named in a foreign key, and v1 had no schedule_id at all.
    _rebuild_legacy_video_items(conn)

    conn.execute(text("DROP TABLE IF EXISTS oauth_states"))
    conn.execute(text("CREATE TABLE oauth_states (state VARCHAR(128) PRIMARY KEY, "
                      "code_verifier VARCHAR(256), created_at DATETIME, used BOOLEAN)"))
    conn.execute(text("DELETE FROM settings WHERE key = 'schema_version'"))

    now = "2026-09-15 10:00:00.000000"
    conn.execute(text(
        "INSERT INTO accounts (id, open_id, relay_account_id, relay_api_key_enc, username, "
        "display_name, is_active, created_at, updated_at, relay_provider) VALUES "
        "(1, 'local:abc', NULL, NULL, NULL, 'Modo manual', 1, :now, :now, NULL), "
        "(2, 'relay:zernio:acc1', 'acc1', :key, 'creator', 'creator', 1, :now, :now, 'zernio')"
    ), {"now": now, "key": encrypt("zernio-secret")})
    schedule_cols = (
        "account_id, enabled, interval_min_minutes, interval_max_minutes, min_gap_minutes, "
        "jitter_seconds, max_per_day, windows, days_of_week, timezone, privacy_level, "
        "disable_comment, disable_duet, disable_stitch, is_aigc, brand_content_toggle, "
        "brand_organic_toggle, sound_boost_enabled, watch_inbox, post_mode"
    )
    conn.execute(text(
        f"INSERT INTO schedules ({schedule_cols}) VALUES "
        "(1, 1, 25, 35, 0, 0, NULL, '[]', '[0,1,2,3,4,5,6]', 'Europe/Madrid', '', "
        "0, 0, 0, 0, 0, 0, 1, 1, 'MANUAL'), "
        "(2, 1, 25, 35, 0, 0, 6, '[]', '[0,1,2,3,4,5,6]', 'Europe/Madrid', '', "
        "0, 0, 0, 0, 0, 0, 0, 1, 'RELAY')"
    ))
    video_cols = ("account_id, file_path, original_name, checksum, size_bytes, caption, status, "
                  "priority, force_publish, attempts, created_at")
    rows = [
        # Local account: one announced, one queued duplicate of the Zernio one, one unique.
        (1, "/m/a.mp4", "a.mp4", "sum-a", 10, "", "AWAITING_MANUAL", 100),
        (1, "/m/b.mp4", "b.mp4", "sum-b", 10, "", "QUEUED", 100),
        (1, "/m/c.mp4", "c.mp4", "sum-c", 10, "texto propio", "QUEUED", 100),
        # Zernio account: one already published, one queued (same file as b).
        (2, "/m/p.mp4", "p.mp4", "sum-p", 10, "", "PUBLISHED", 100),
        (2, "/m/b.mp4", "b.mp4", "sum-b", 10, "", "QUEUED", 100),
        (2, "/m/x.mp4", "x.mp4", "sum-x", 10, "", "CANCELLED", 100),
    ]
    for row in rows:
        conn.execute(text(
            f"INSERT INTO video_items ({video_cols}) VALUES "
            "(:acc, :path, :name, :sum, :size, :caption, :status, :prio, 0, 0, :now)"
        ), {"acc": row[0], "path": row[1], "name": row[2], "sum": row[3], "size": row[4],
            "caption": row[5], "status": row[6], "prio": row[7], "now": now})


def _add_pre_v8_columns(conn) -> None:
    """Restore the per-queue watch folder switch v8 removed."""
    conn.execute(text(
        "ALTER TABLE schedules ADD COLUMN watch_inbox BOOLEAN NOT NULL DEFAULT 1"))


def _add_pre_v6_columns(conn) -> None:
    """Restore the per-queue ``min_gap_minutes`` v6 moved onto the account."""
    conn.execute(text(
        "ALTER TABLE schedules ADD COLUMN min_gap_minutes INTEGER NOT NULL DEFAULT 15"))


def _add_pre_v4_columns(conn) -> None:
    """Restore the drafts / privacy / Sound Boost columns v4 removed.

    The legacy fixtures start from the *current* schema and walk it backwards,
    so anything a migration deletes has to be put back here first.
    """
    conn.execute(text(
        "ALTER TABLE schedules ADD COLUMN to_drafts BOOLEAN NOT NULL DEFAULT 0"))
    conn.execute(text(
        "ALTER TABLE schedules ADD COLUMN privacy_level VARCHAR(48) "
        "NOT NULL DEFAULT 'PUBLIC_TO_EVERYONE'"))
    conn.execute(text(
        "ALTER TABLE schedules ADD COLUMN sound_boost_enabled BOOLEAN NOT NULL DEFAULT 0"))
    conn.execute(text("DROP TABLE IF EXISTS sounds"))
    conn.execute(text("""
        CREATE TABLE sounds (
            id INTEGER NOT NULL,
            music_id VARCHAR(128) NOT NULL,
            title TEXT NOT NULL,
            author TEXT,
            url TEXT,
            rank INTEGER NOT NULL,
            region VARCHAR(8) NOT NULL,
            source VARCHAR(32) NOT NULL,
            fetched_at DATETIME NOT NULL,
            times_used INTEGER NOT NULL,
            PRIMARY KEY (id)
        )
    """))
    conn.execute(text(
        "INSERT INTO sounds (music_id, title, author, url, rank, region, source, "
        "fetched_at, times_used) VALUES ('7123', 'Viral', 'A', '', 1, 'ES', 'manual', "
        "'2026-09-15 10:00:00.000000', 3)"))


def _rebuild_legacy_video_items(conn) -> None:
    """The pre-v3 video_items table: no schedule_id, v1 column names."""
    conn.execute(text("DROP TABLE video_items"))
    conn.execute(text("""
        CREATE TABLE video_items (
            id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            original_name TEXT NOT NULL,
            checksum VARCHAR(64) NOT NULL,
            size_bytes INTEGER NOT NULL,
            caption TEXT NOT NULL,
            rendered_caption TEXT,
            status VARCHAR(16) NOT NULL,
            priority INTEGER NOT NULL,
            force_publish BOOLEAN NOT NULL,
            scheduled_at DATETIME,
            publish_id VARCHAR(255),
            post_id VARCHAR(255),
            post_url TEXT,
            suggested_sound JSON,
            probe JSON,
            preflight_errors JSON,
            attempts INTEGER NOT NULL,
            last_error TEXT,
            next_status_check_at DATETIME,
            upload_started_at DATETIME,
            announced_at DATETIME,
            created_at DATETIME NOT NULL,
            published_at DATETIME,
            PRIMARY KEY (id),
            CONSTRAINT uq_account_checksum UNIQUE (account_id, checksum),
            FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
        )
    """))
    conn.execute(text("CREATE INDEX ix_video_items_account_id ON video_items (account_id)"))
    conn.execute(text("CREATE INDEX ix_video_items_status ON video_items (status)"))
    conn.execute(text("CREATE INDEX ix_video_items_checksum ON video_items (checksum)"))
    conn.execute(text("CREATE INDEX ix_video_items_scheduled_at ON video_items (scheduled_at)"))


def test_v1_database_is_migrated_to_zernio_only(fresh_db):
    with engine.begin() as conn:
        _build_legacy_database(conn)
    assert "open_id" in _columns("accounts") and "oauth_states" in _tables()

    actions = migrate()
    assert any("moved to account 2" in a for a in actions), actions

    # Only the Zernio account survives, under its new column names.
    assert "open_id" not in _columns("accounts")
    assert {"external_id", "zernio_account_id", "api_key_enc", "profile_name"} <= _columns("accounts")
    assert not {"relay_provider", "relay_url", "scopes", "access_token_enc"} & _columns("accounts")
    assert "post_mode" not in _columns("schedules")
    # v4 took the drafts / privacy / Sound Boost columns and the sounds table.
    assert not {"to_drafts", "privacy_level", "sound_boost_enabled"} & _columns("schedules")
    # v6 took the per-queue separation: only the account's is left.
    assert "min_gap_minutes" not in _columns("schedules")
    assert "min_gap_minutes" in _columns("accounts")
    assert "suggested_sound" not in _columns("video_items")
    assert "sounds" not in _tables()
    assert "publish_id" not in _columns("video_items") and "zernio_post_id" in _columns("video_items")
    assert "oauth_states" not in _tables()
    # v8: the library replaced the per-queue watch folder.
    assert "watch_inbox" not in _columns("schedules")
    assert "library_item_id" in _columns("video_items")

    with session_scope() as session:
        account = session.query(Account).one()
        assert account.external_id == "zernio:acc1" and account.zernio_account_id == "acc1"
        assert decrypt(account.api_key_enc) == "zernio-secret"
        # v3: the single schedule became the account's first named queue, and
        # the account gained the limits its queues now share.
        schedule = account.schedules[0]
        assert len(account.schedules) == 1
        assert schedule.name == "Cola principal" and schedule.position == 0
        assert account.max_per_day == 15 and account.min_gap_minutes == 15

        videos = {v.original_name: v for v in session.query(VideoItem).all()}
        # b.mp4 existed in both queues -> the legacy copy was dropped; the
        # cancelled row is gone; the announced one is queued again.
        assert set(videos) == {"a.mp4", "b.mp4", "c.mp4", "p.mp4"}
        assert all(v.account_id == account.id for v in videos.values())
        # Every surviving video is attached to that queue; none is orphaned.
        assert all(v.schedule_id == schedule.id for v in videos.values())
        assert videos["a.mp4"].status == VideoStatus.QUEUED
        assert videos["c.mp4"].caption == "texto propio"
        assert videos["p.mp4"].status == VideoStatus.PUBLISHED

    # Running it again is a no-op, and the version is recorded.
    assert migrate() == []
    with engine.connect() as conn:
        raw = conn.execute(text("SELECT value FROM settings WHERE key='schema_version'")).scalar()
    assert str(SCHEMA_VERSION) in raw


def test_v1_database_without_zernio_account_drops_the_legacy_queue(fresh_db):
    with engine.begin() as conn:
        _build_legacy_database(conn)
        conn.execute(text("DELETE FROM accounts WHERE id = 2"))
    actions = migrate()
    assert any("removed with its queue" in a for a in actions), actions
    with session_scope() as session:
        assert session.query(Account).count() == 0
        assert session.query(VideoItem).count() == 0


def test_v3_database_loses_drafts_privacy_and_sound_boost(fresh_db):
    """A v3 install keeps its videos; only the dead columns and table go."""
    with engine.begin() as conn:
        _add_pre_v8_columns(conn)
        _add_pre_v4_columns(conn)
        _add_pre_v6_columns(conn)
        conn.execute(text("ALTER TABLE video_items ADD COLUMN suggested_sound JSON"))
        # 3.1 added the automatic-text columns; a v3 database has none of them.
        conn.execute(text("DROP INDEX IF EXISTS ix_video_items_caption_status"))
        for column in ("caption_status", "caption_source", "caption_error",
                       "caption_generated_at", "caption_requested_at", "transcript",
                       "auto_hashtags"):
            conn.execute(text(f"ALTER TABLE video_items DROP COLUMN {column}"))
        now = "2026-09-15 10:00:00.000000"
        conn.execute(text(
            "INSERT INTO accounts (id, external_id, zernio_account_id, api_key_enc, username, "
            "display_name, is_active, max_per_day, min_gap_minutes, created_at, updated_at) "
            "VALUES (1, 'zernio:acc1', 'acc1', NULL, 'creator', 'creator', 1, 15, 15, :now, :now)"
        ), {"now": now})
        conn.execute(text(
            "INSERT INTO schedules (id, account_id, name, position, enabled, "
            "interval_min_minutes, interval_max_minutes, min_gap_minutes, jitter_seconds, "
            "max_per_day, windows, days_of_week, timezone, to_drafts, privacy_level, "
            "disable_comment, disable_duet, disable_stitch, is_aigc, brand_content_toggle, "
            "brand_organic_toggle, sound_boost_enabled, watch_inbox) VALUES "
            "(1, 1, 'Cola principal', 0, 1, 55, 75, 15, 90, 6, '[]', '[0,1,2,3,4,5,6]', "
            "'Europe/Madrid', 1, 'SELF_ONLY', 0, 0, 0, 0, 0, 0, 1, 1)"
        ))
        conn.execute(text(
            "INSERT INTO video_items (account_id, schedule_id, file_path, original_name, "
            "checksum, size_bytes, caption, status, priority, force_publish, attempts, "
            "created_at, suggested_sound) VALUES (1, 1, '/m/v.mp4', 'v.mp4', 'sum-v', 10, "
            "'hola', 'PUBLISHED', 100, 0, 0, :now, '{\"title\": \"Viral\"}')"
        ), {"now": now})
        conn.execute(text("UPDATE settings SET value = '{\"value\": 3}' "
                          "WHERE key = 'schema_version'"))
        conn.execute(text(
            "INSERT OR IGNORE INTO settings (key, value, updated_at) "
            "VALUES ('schema_version', '{\"value\": 3}', :now)"
        ), {"now": now})

    actions = migrate()
    assert any("sounds table dropped" in a for a in actions), actions
    assert not {"to_drafts", "privacy_level", "sound_boost_enabled"} & _columns("schedules")
    assert "suggested_sound" not in _columns("video_items")
    assert "sounds" not in _tables()
    # The new tables are there and the video survived untouched.
    assert {"video_metrics", "comments"} <= _tables()

    with session_scope() as session:
        video = session.query(VideoItem).one()
        assert video.original_name == "v.mp4" and video.caption == "hola"
        assert video.status == VideoStatus.PUBLISHED
        # The 3.1 columns arrived with their defaults, and their index.
        assert video.caption_status == "none" and video.auto_hashtags is None
        assert session.query(Schedule).one().name == "Cola principal"
    with engine.connect() as conn:
        indexes = {r[1] for r in conn.execute(text("PRAGMA index_list(video_items)"))}
    assert "ix_video_items_caption_status" in indexes

    assert migrate() == []


def test_v2_database_gains_queues(fresh_db):
    """A v2 install (one schedule per account) becomes a v3 one-queue account."""
    with engine.begin() as conn:
        _add_pre_v8_columns(conn)
        conn.execute(text("ALTER TABLE accounts DROP COLUMN max_per_day"))
        conn.execute(text("ALTER TABLE accounts DROP COLUMN min_gap_minutes"))
        conn.execute(text("ALTER TABLE schedules DROP COLUMN name"))
        conn.execute(text("ALTER TABLE schedules DROP COLUMN position"))
        _add_pre_v4_columns(conn)
        _add_pre_v6_columns(conn)
        _rebuild_legacy_video_items(conn)
        conn.execute(text("ALTER TABLE video_items RENAME COLUMN publish_id TO zernio_post_id"))
        conn.execute(text("ALTER TABLE video_items DROP COLUMN post_id"))
        conn.execute(text("ALTER TABLE video_items DROP COLUMN announced_at"))
        conn.execute(text("DROP INDEX IF EXISTS ix_schedules_account_id"))
        conn.execute(text("CREATE UNIQUE INDEX ix_schedules_account_id ON schedules (account_id)"))
        now = "2026-09-15 10:00:00.000000"
        conn.execute(text(
            "INSERT INTO accounts (id, external_id, zernio_account_id, api_key_enc, username, "
            "display_name, is_active, created_at, updated_at) VALUES "
            "(1, 'zernio:acc1', 'acc1', NULL, 'creator', 'creator', 1, :now, :now)"
        ), {"now": now})
        conn.execute(text(
            "INSERT INTO schedules (account_id, enabled, interval_min_minutes, "
            "interval_max_minutes, min_gap_minutes, jitter_seconds, max_per_day, windows, "
            "days_of_week, timezone, to_drafts, privacy_level, disable_comment, disable_duet, "
            "disable_stitch, is_aigc, brand_content_toggle, brand_organic_toggle, "
            "sound_boost_enabled, watch_inbox) VALUES "
            "(1, 1, 55, 75, 15, 90, 6, '[]', '[0,1,2,3,4,5,6]', 'Europe/Madrid', 0, "
            "'PUBLIC_TO_EVERYONE', 0, 0, 0, 0, 0, 0, 0, 1)"
        ))
        conn.execute(text(
            "INSERT INTO video_items (account_id, file_path, original_name, checksum, "
            "size_bytes, caption, status, priority, force_publish, attempts, created_at) "
            "VALUES (1, '/m/v.mp4', 'v.mp4', 'sum-v', 10, '', 'QUEUED', 100, 0, 0, :now)"
        ), {"now": now})
        conn.execute(text("UPDATE settings SET value = '{\"value\": 2}' "
                          "WHERE key = 'schema_version'"))
        conn.execute(text(
            "INSERT OR IGNORE INTO settings (key, value, updated_at) "
            "VALUES ('schema_version', '{\"value\": 2}', :now)"
        ), {"now": now})

    actions = migrate()
    assert any("attached to their account" in a for a in actions), actions

    with session_scope() as session:
        account = session.query(Account).one()
        assert [s.name for s in account.schedules] == ["Cola principal"]
        video = session.query(VideoItem).one()
        assert video.schedule_id == account.schedules[0].id

    # A second queue is now possible, which is the whole point of v3.
    with session_scope() as session:
        account = session.query(Account).one()
        session.add(Schedule(account_id=account.id, name="Noche", position=1))
    with session_scope() as session:
        assert session.query(Schedule).count() == 2

    assert migrate() == []


def test_v4_database_starts_labelling_ai_content(fresh_db):
    """v4 had the AI label off by default; v5 turns it on and copies it down.

    What is already published keeps its own history (``is_aigc`` stays NULL and
    means "whatever the queue declared back then"); what is still waiting gets
    the label its queue now carries, which is what the user will see in the
    queue view.
    """
    with engine.begin() as conn:
        _add_pre_v8_columns(conn)
        _add_pre_v6_columns(conn)
        conn.execute(text("ALTER TABLE video_items DROP COLUMN is_aigc"))
        conn.execute(text("ALTER TABLE video_items DROP COLUMN delegated_at"))
        now = "2026-09-16 10:00:00.000000"
        conn.execute(text(
            "INSERT INTO accounts (id, external_id, zernio_account_id, api_key_enc, username, "
            "display_name, is_active, max_per_day, min_gap_minutes, created_at, updated_at) "
            "VALUES (1, 'zernio:acc1', 'acc1', NULL, 'creator', 'creator', 1, 15, 15, :now, :now)"
        ), {"now": now})
        conn.execute(text(
            "INSERT INTO schedules (id, account_id, name, position, enabled, "
            "interval_min_minutes, interval_max_minutes, min_gap_minutes, jitter_seconds, "
            "max_per_day, windows, days_of_week, timezone, disable_comment, disable_duet, "
            "disable_stitch, is_aigc, brand_content_toggle, brand_organic_toggle, watch_inbox) "
            "VALUES (1, 1, 'Cola principal', 0, 1, 55, 75, 15, 90, 6, '[]', "
            "'[0,1,2,3,4,5,6]', 'Europe/Madrid', 0, 0, 0, 0, 0, 0, 1)"
        ))
        for vid, name, status in ((1, "pendiente.mp4", "QUEUED"), (2, "viejo.mp4", "PUBLISHED")):
            conn.execute(text(
                "INSERT INTO video_items (id, account_id, schedule_id, file_path, "
                "original_name, checksum, size_bytes, caption, caption_status, status, "
                "priority, force_publish, attempts, created_at) VALUES "
                f"(:id, 1, 1, '/m/{name}', :name, :sum, 10, '', 'none', :status, 100, 0, "
                "0, :now)"
            ), {"id": vid, "name": name, "sum": f"sum-{vid}", "status": status, "now": now})
        conn.execute(text("UPDATE settings SET value = '{\"value\": 4}' "
                          "WHERE key = 'schema_version'"))
        conn.execute(text(
            "INSERT OR IGNORE INTO settings (key, value, updated_at) "
            "VALUES ('schema_version', '{\"value\": 4}', :now)"
        ), {"now": now})

    actions = migrate()
    assert any("AI-generated" in a for a in actions), actions

    with session_scope() as session:
        assert session.query(Schedule).one().is_aigc is True
        by_name = {v.original_name: v for v in session.query(VideoItem).all()}
        assert by_name["pendiente.mp4"].is_aigc is True
        assert by_name["viejo.mp4"].is_aigc is None, "lo ya publicado no se reescribe"

    assert migrate() == []


def test_v6_gives_back_the_hours_a_catch_up_poll_flattened(fresh_db):
    """Only the batch stamped all at once is repaired; real hours are kept.

    The rows mirror the shape this was found in: three delegated videos that
    Zernio published overnight and one poll dated at 07:41, one delegated
    video that was polled live a few minutes after its hour, and one the app
    published itself.
    """
    with engine.begin() as conn:
        _add_pre_v8_columns(conn)
        now = "2026-09-17 09:00:00.000000"
        conn.execute(text(
            "INSERT INTO accounts (id, external_id, zernio_account_id, api_key_enc, username, "
            "display_name, is_active, max_per_day, min_gap_minutes, created_at, updated_at) "
            "VALUES (1, 'zernio:acc1', 'acc1', NULL, 'creator', 'creator', 1, 15, 15, :now, :now)"
        ), {"now": now})
        conn.execute(text(
            "INSERT INTO schedules (id, account_id, name, position, enabled, "
            "interval_min_minutes, interval_max_minutes, jitter_seconds, max_per_day, windows, "
            "days_of_week, timezone, disable_comment, disable_duet, disable_stitch, is_aigc, "
            "brand_content_toggle, brand_organic_toggle, watch_inbox) VALUES "
            "(1, 1, 'Cola principal', 0, 1, 55, 75, 90, 6, '[]', '[0,1,2,3,4,5,6]', "
            "'Europe/Madrid', 0, 0, 0, 1, 0, 0, 1)"
        ))
        rows = (
            # id, name,         scheduled,             published,             delegated?
            (1, "madrugada.mp4", "05:01:12", "07:41:42", True),   # caught up
            (2, "manana.mp4",    "06:11:03", "07:41:42", True),   # caught up
            (3, "tarde.mp4",     "07:14:27", "07:41:43", True),   # caught up (27 min)
            (4, "sola.mp4",      "20:56:20", "21:02:03", True),   # polled live, alone
            (5, "local.mp4",     "19:38:01", "19:41:04", False),  # this app published it
        )
        for vid, name, sched, pub, delegated in rows:
            day = "2026-09-16" if vid >= 4 else "2026-09-17"
            conn.execute(text(
                "INSERT INTO video_items (id, account_id, schedule_id, file_path, "
                "original_name, checksum, size_bytes, caption, caption_status, status, "
                "priority, force_publish, attempts, created_at, scheduled_at, published_at, "
                "delegated_at) VALUES (:id, 1, 1, :path, :name, :sum, 10, '', 'none', "
                "'PUBLISHED', 100, 0, 0, :now, :sched, :pub, :deleg)"
            ), {"id": vid, "path": f"/m/{name}", "name": name, "sum": f"sum-{vid}",
                "now": now, "sched": f"{day} {sched}.000000", "pub": f"{day} {pub}.000000",
                "deleg": f"{day} 00:00:00.000000" if delegated else None})
        conn.execute(text("UPDATE settings SET value = '{\"value\": 6}' "
                          "WHERE key = 'schema_version'"))
        conn.execute(text(
            "INSERT OR IGNORE INTO settings (key, value, updated_at) "
            "VALUES ('schema_version', '{\"value\": 6}', :now)"
        ), {"now": now})

    actions = migrate()
    assert any("real publication hour" in a for a in actions), actions

    with session_scope() as session:
        by_name = {v.original_name: v for v in session.query(VideoItem).all()}
        for name in ("madrugada.mp4", "manana.mp4", "tarde.mp4"):
            item = by_name[name]
            assert item.published_at == item.scheduled_at, f"{name} conserva la hora del sondeo"
        # Three videos of one account no longer claim the same instant.
        stamps = [by_name[n].published_at for n in ("madrugada.mp4", "manana.mp4", "tarde.mp4")]
        assert len(set(stamps)) == 3
        # A delegated post seen live, and one this app published, are untouched.
        assert by_name["sola.mp4"].published_at != by_name["sola.mp4"].scheduled_at
        assert by_name["local.mp4"].published_at != by_name["local.mp4"].scheduled_at

    assert migrate() == []


def test_v5_lifts_the_queue_separation_onto_the_account(fresh_db):
    """The widest gap any queue had becomes the account's, then the column goes.

    A queue used to carry its own floor under the random interval while the
    account carried the distance between queues. Keeping the largest of the
    two means no account comes out of the upgrade publishing closer together
    than it did before.
    """
    with engine.begin() as conn:
        _add_pre_v8_columns(conn)
        _add_pre_v6_columns(conn)
        now = "2026-09-17 10:00:00.000000"
        conn.execute(text(
            "INSERT INTO accounts (id, external_id, zernio_account_id, api_key_enc, username, "
            "display_name, is_active, max_per_day, min_gap_minutes, created_at, updated_at) "
            "VALUES (1, 'zernio:acc1', 'acc1', NULL, 'creator', 'creator', 1, 15, 15, :now, :now),"
            "       (2, 'zernio:acc2', 'acc2', NULL, 'otra', 'otra', 1, 15, 30, :now, :now)"
        ), {"now": now})
        rows = (
            # account 1: a queue insists on 90 min, well above the account's 15.
            (1, 1, "Cola principal", 0, 90), (2, 1, "Noche", 1, 20),
            # account 2: nothing beats the 30 the account already had.
            (3, 2, "Cola principal", 0, 10),
        )
        for sid, account_id, name, position, gap in rows:
            conn.execute(text(
                "INSERT INTO schedules (id, account_id, name, position, enabled, "
                "interval_min_minutes, interval_max_minutes, min_gap_minutes, jitter_seconds, "
                "max_per_day, windows, days_of_week, timezone, disable_comment, disable_duet, "
                "disable_stitch, is_aigc, brand_content_toggle, brand_organic_toggle, "
                "watch_inbox) VALUES (:id, :acc, :name, :pos, 1, 55, 75, :gap, 90, 6, '[]', "
                "'[0,1,2,3,4,5,6]', 'Europe/Madrid', 0, 0, 0, 1, 0, 0, 1)"
            ), {"id": sid, "acc": account_id, "name": name, "pos": position, "gap": gap})
        conn.execute(text("UPDATE settings SET value = '{\"value\": 5}' "
                          "WHERE key = 'schema_version'"))
        conn.execute(text(
            "INSERT OR IGNORE INTO settings (key, value, updated_at) "
            "VALUES ('schema_version', '{\"value\": 5}', :now)"
        ), {"now": now})

    actions = migrate()
    assert any("largest gap" in a for a in actions), actions
    assert "min_gap_minutes" not in _columns("schedules")

    with session_scope() as session:
        by_id = {a.id: a for a in session.query(Account).all()}
        assert by_id[1].min_gap_minutes == 90, "la cola más separada manda"
        assert by_id[2].min_gap_minutes == 30, "una cola más apretada no baja la cuenta"
        assert session.query(Schedule).count() == 3, "ninguna cola se pierde"

    assert migrate() == []


def test_v7_database_trades_the_watch_folder_for_the_library(fresh_db):
    """v8: the per-queue inbox switch goes; the library table and link arrive."""
    with engine.begin() as conn:
        _add_pre_v8_columns(conn)
        # SQLite cannot drop a column named in a foreign key; the v1 fixture,
        # which rebuilds video_items, is the one that sees library_item_id added.
        conn.execute(text("DROP TABLE library_items"))
        conn.execute(text(
            "UPDATE settings SET value = '{\"value\": 7}' WHERE key = 'schema_version'"))
    assert "watch_inbox" in _columns("schedules")

    actions = init_db()
    assert any("watch_inbox" in a for a in actions)
    assert "watch_inbox" not in _columns("schedules")
    assert "library_items" in _tables()
    assert migrate() == []


def test_v9_asks_again_about_delegated_posts_failed_unseen(fresh_db):
    """Only the 48 h timeouts with a Zernio post go back; real failures stay."""
    with session_scope() as session:
        account = Account(external_id="zernio:acc1", zernio_account_id="acc1",
                          username="creator", display_name="creator")
        session.add(account)
        session.flush()
        queue = Schedule(account_id=account.id, name="Cola principal")
        session.add(queue)
        session.flush()
        rows = (
            ("perdido.mp4", "post-1",
             "Zernio no confirmó la publicación 48 h después de la hora programada. "
             "Míralo en el panel de Zernio."),
            ("rechazado.mp4", "post-2", "TikTok rechazó el vídeo"),
            ("sin-post.mp4", None,
             "Zernio no confirmó la publicación 48 h después de la hora programada."),
        )
        for i, (name, post_id, error) in enumerate(rows):
            session.add(VideoItem(
                account_id=account.id, schedule_id=queue.id, file_path=f"/m/{name}",
                original_name=name, checksum=f"sum-{i}", size_bytes=10,
                status=VideoStatus.FAILED, zernio_post_id=post_id, last_error=error))
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE settings SET value = '{\"value\": 8}' WHERE key = 'schema_version'"))

    actions = migrate()
    assert any("asked about" in a for a in actions), actions

    with session_scope() as session:
        by_name = {v.original_name: v for v in session.query(VideoItem).all()}
        assert by_name["perdido.mp4"].status == VideoStatus.DELEGATED
        assert by_name["perdido.mp4"].last_error is None
        assert by_name["rechazado.mp4"].status == VideoStatus.FAILED
        assert by_name["sin-post.mp4"].status == VideoStatus.FAILED

    assert migrate() == []


def test_v10_follows_videos_saved_through_the_old_project_folder(fresh_db, tmp_path):
    """Missing paths found in this install's library move; the rest stay as they are."""
    library = Path(get_settings().media_library_dir)
    (library / "serie").mkdir(parents=True, exist_ok=True)
    (library / "serie" / "movido.mp4").write_bytes(b"mp4")
    elsewhere = tmp_path / "media" / "library" / "otra" / "sigue.mp4"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_bytes(b"mp4")
    paths = {
        "movido.mp4": "/carpeta/vieja/media/library/serie/movido.mp4",
        "perdido.mp4": "/carpeta/vieja/media/library/serie/perdido.mp4",
        "sigue.mp4": str(elsewhere),
    }
    with session_scope() as session:
        account = Account(external_id="zernio:acc1", zernio_account_id="acc1",
                          username="creator", display_name="creator")
        session.add(account)
        session.flush()
        queue = Schedule(account_id=account.id, name="Cola principal")
        session.add(queue)
        session.flush()
        for i, (name, path) in enumerate(paths.items()):
            session.add(LibraryItem(folder="serie", file_path=path, original_name=name,
                                    checksum=f"sum-{i}", size_bytes=3))
            session.add(VideoItem(account_id=account.id, schedule_id=queue.id, file_path=path,
                                  original_name=name, checksum=f"sum-{i}", size_bytes=3,
                                  status=VideoStatus.PUBLISHED))
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE settings SET value = '{\"value\": 9}' WHERE key = 'schema_version'"))

    actions = migrate()
    assert any("2 file path(s)" in a for a in actions), actions

    moved = str(library / "serie" / "movido.mp4")
    with session_scope() as session:
        for model in (LibraryItem, VideoItem):
            by_name = {r.original_name: r.file_path for r in session.query(model).all()}
            assert by_name == {**paths, "movido.mp4": moved}

    assert migrate() == []
