"""Database engine, sessions and schema migrations.

There is no Alembic: this is a single-user local tool. Instead, ``migrate()``
brings any existing ``scheduler.db`` up to the current models on start-up:

* ``add_missing_columns`` appends columns the models gained (nullable or with
  a default), so upgrades never break an installation that already has data;
* numbered migrations handle the rare structural change, like the version 2
  clean-up that left Zernio as the only publishing path, the version 3 move
  from one schedule per account to several ("colas"), the version 4 clean-up
  that removed drafts, privacy levels and Sound Boost, the version 5 switch
  to labelling AI content by default, the version 6 move of the minimum
  separation between posts onto the account, the version 7 repair of the
  publication times a catch-up poll had flattened, the version 8 removal
  of the per-queue watch folder when the shared library replaced it, or the
  version 10 repair of file paths left behind by the project folder rename.

SQLite ≥ 3.35 is required (RENAME COLUMN / DROP COLUMN); Python 3.11+ ships it.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .models import Base

log = logging.getLogger("scheduler")

SCHEMA_VERSION = 10
_VERSION_KEY = "schema_version"

_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    echo=False,
    future=True,
    # The scheduler thread and the request handlers share this engine.
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver level
    cur = dbapi_conn.cursor()
    # WAL lets the scheduler write while the UI reads.
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


# autoflush=True on purpose: the scheduler mutates objects and then queries
# the queue in the same transaction, and must not read stale rows.
SessionLocal = sessionmaker(bind=engine, autoflush=True, expire_on_commit=False, future=True)


def init_db() -> list[str]:
    """Create missing tables, then migrate whatever already existed."""
    Base.metadata.create_all(engine)
    return migrate()


def init_additive(bind=None) -> list[str]:
    """Only the part of :func:`init_db` that an older running copy survives.

    New tables and new columns, nothing renamed or dropped, and the schema
    version left alone so the app's next start still runs the numbered
    migrations. tools/library_copy.py uses it: it can run while the previous
    version of the app is still serving, which must keep working. ``bind``
    points it at another database (the scratch copy a dry run works on).
    """
    target = bind or engine
    Base.metadata.create_all(target)
    with target.begin() as conn:
        return add_missing_columns(conn)


# --------------------------------------------------------------------------- #
# Introspection helpers (raw SQL: the ORM models describe the *target* schema)
# --------------------------------------------------------------------------- #

def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(text(f'PRAGMA table_info("{table}")'))}


def _tables(conn) -> set[str]:
    rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
    return {row[0] for row in rows}


def _rename(conn, table: str, old: str, new: str) -> str | None:
    cols = _columns(conn, table)
    if old not in cols:
        return None
    if new in cols:
        conn.execute(text(f'ALTER TABLE "{table}" DROP COLUMN "{old}"'))
        return f"{table}.{old} dropped (already had {new})"
    conn.execute(text(f'ALTER TABLE "{table}" RENAME COLUMN "{old}" TO "{new}"'))
    return f"{table}.{old} -> {new}"


def _drop(conn, table: str, column: str) -> str | None:
    if column not in _columns(conn, table):
        return None
    conn.execute(text(f'ALTER TABLE "{table}" DROP COLUMN "{column}"'))
    return f"{table}.{column} dropped"


def _sql_literal(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def _now_text() -> str:
    # The exact format SQLAlchemy's SQLite DateTime type reads back.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


# --------------------------------------------------------------------------- #
# Versioning
# --------------------------------------------------------------------------- #

def _schema_version(conn) -> int:
    row = conn.execute(
        text("SELECT value FROM settings WHERE key = :k"), {"k": _VERSION_KEY}
    ).fetchone()
    if row and row[0]:
        try:
            return int(json.loads(row[0]).get("value", 0))
        except (ValueError, AttributeError, TypeError):
            return 0
    # No version recorded: either a database from before versioning (v1, which
    # still has the old OAuth `open_id` column) or a brand-new one.
    if "accounts" in _tables(conn) and "open_id" in _columns(conn, "accounts"):
        return 1
    return SCHEMA_VERSION


def _set_schema_version(conn, version: int) -> None:
    conn.execute(
        text(
            "INSERT INTO settings (key, value, updated_at) VALUES (:k, :v, :now) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
        ),
        {"k": _VERSION_KEY, "v": json.dumps({"value": version}), "now": _now_text()},
    )


def migrate() -> list[str]:
    """Bring the database up to the current schema. Returns what was done."""
    actions: list[str] = []
    with engine.begin() as conn:
        version = _schema_version(conn)
        if version < 2:
            actions += migrate_v1_to_v2(conn)
        # Before the numbered migrations that *fill* new columns, and after the
        # ones that rename or drop old ones.
        actions += add_missing_columns(conn)
        if version < 3:
            actions += migrate_v2_to_v3(conn)
        if version < 4:
            actions += migrate_v3_to_v4(conn)
        if version < 5:
            actions += migrate_v4_to_v5(conn)
        if version < 6:
            actions += migrate_v5_to_v6(conn)
        if version < 7:
            actions += migrate_v6_to_v7(conn)
        if version < 8:
            actions += migrate_v7_to_v8(conn)
        if version < 9:
            actions += migrate_v8_to_v9(conn)
        if version < 10:
            actions += migrate_v9_to_v10(conn)
        _set_schema_version(conn, SCHEMA_VERSION)
    if actions:
        log.info("database migrated: %s", "; ".join(actions))
    return actions


def add_missing_columns(conn=None) -> list[str]:
    """Append columns the models gained since the table was created."""
    if conn is None:
        with engine.begin() as own:
            return add_missing_columns(own)
    inspector = inspect(conn)
    added: list[str] = []
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            ddl = (
                f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" '
                f"{column.type.compile(engine.dialect)}"
            )
            default = column.default
            if default is not None and default.is_scalar:
                ddl += f" DEFAULT {_sql_literal(default.arg)}"
            conn.execute(text(ddl))
            if column.index:
                # create_all() only builds indexes with new tables; a column
                # that arrives later has to bring its own.
                conn.execute(text(
                    f'CREATE INDEX IF NOT EXISTS "ix_{table.name}_{column.name}" '
                    f'ON "{table.name}" ("{column.name}")'
                ))
            added.append(f"{table.name}.{column.name} added")
    return added


# --------------------------------------------------------------------------- #
# v1 -> v2: Zernio becomes the only publishing path
# --------------------------------------------------------------------------- #

def migrate_v1_to_v2(conn) -> list[str]:
    """Consolidate accounts onto Zernio and drop everything OAuth / manual.

    * Videos of removed accounts (local "manual" ones, direct TikTok OAuth
      ones) move to the first Zernio account; files the Zernio account already
      queued are dropped as duplicates. Without any Zernio account they are
      deleted with their account: the files stay on disk and the watch folder
      re-imports them once a Zernio account exists.
    * Columns that only made sense for OAuth or the manual mode are dropped,
      and the Zernio ones get plain names.
    """
    done: list[str] = []
    tables = _tables(conn)
    if "accounts" not in tables:
        return done

    zernio = conn.execute(text(
        "SELECT id FROM accounts WHERE open_id LIKE 'relay:zernio:%' ORDER BY id LIMIT 1"
    )).scalar()
    legacy_ids = [row[0] for row in conn.execute(text(
        "SELECT id FROM accounts WHERE open_id NOT LIKE 'relay:zernio:%' ORDER BY id"
    ))]
    for legacy in legacy_ids:
        if zernio is not None:
            dup = conn.execute(text(
                "DELETE FROM video_items WHERE account_id = :legacy AND checksum IN "
                "(SELECT checksum FROM video_items WHERE account_id = :target)"
            ), {"legacy": legacy, "target": zernio}).rowcount
            moved = conn.execute(text(
                "UPDATE video_items SET account_id = :target, "
                "status = CASE WHEN status IN ('PUBLISHED', 'FAILED') THEN status ELSE 'QUEUED' END, "
                "scheduled_at = NULL, publish_id = NULL, force_publish = 0 "
                "WHERE account_id = :legacy"
            ), {"legacy": legacy, "target": zernio}).rowcount
            done.append(f"account {legacy}: {moved} video(s) moved to account {zernio}, "
                        f"{dup} duplicate(s) dropped")
        else:
            done.append(f"account {legacy} removed with its queue (no Zernio account yet)")
        # Cascades to its schedule (and to its videos when none were moved).
        conn.execute(text("DELETE FROM accounts WHERE id = :id"), {"id": legacy})

    if "video_items" in tables:
        conn.execute(text("DELETE FROM video_items WHERE status = 'CANCELLED'"))
        conn.execute(text(
            "UPDATE video_items SET status = 'QUEUED', scheduled_at = NULL, publish_id = NULL "
            "WHERE status = 'AWAITING_MANUAL'"
        ))
        for action in (
            _rename(conn, "video_items", "publish_id", "zernio_post_id"),
            _drop(conn, "video_items", "announced_at"),
            _drop(conn, "video_items", "post_id"),
        ):
            if action:
                done.append(action)

    for action in (
        _rename(conn, "accounts", "open_id", "external_id"),
        _rename(conn, "accounts", "relay_account_id", "zernio_account_id"),
        _rename(conn, "accounts", "relay_api_key_enc", "api_key_enc"),
    ):
        if action:
            done.append(action)
    conn.execute(text(
        "UPDATE accounts SET external_id = 'zernio:' || zernio_account_id "
        "WHERE zernio_account_id IS NOT NULL"
    ))
    for column in ("union_id", "avatar_url", "access_token_enc", "refresh_token_enc",
                   "access_expires_at", "refresh_expires_at", "scopes", "creator_info",
                   "creator_info_at", "relay_provider", "relay_url"):
        action = _drop(conn, "accounts", column)
        if action:
            done.append(action)
    conn.execute(text("DROP INDEX IF EXISTS ix_accounts_open_id"))
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_accounts_external_id ON accounts (external_id)"
    ))

    if "schedules" in tables:
        for action in (_drop(conn, "schedules", "post_mode"),
                       _drop(conn, "schedules", "video_cover_timestamp_ms")):
            if action:
                done.append(action)
        conn.execute(text(
            "UPDATE schedules SET privacy_level = 'PUBLIC_TO_EVERYONE' "
            "WHERE privacy_level IS NULL OR privacy_level = ''"
        ))

    if "oauth_states" in tables:
        conn.execute(text("DROP TABLE oauth_states"))
        done.append("oauth_states dropped")
    return done


# --------------------------------------------------------------------------- #
# v2 -> v3: an account may have several queues
# --------------------------------------------------------------------------- #

def migrate_v2_to_v3(conn) -> list[str]:
    """Turn the one-schedule-per-account rule into one-or-more.

    * the UNIQUE index on ``schedules.account_id`` becomes an ordinary one, so a
      second queue can be added;
    * every video is attached to the queue that owns it (until now there was
      exactly one per account, so the mapping is unambiguous);
    * accounts without any schedule — impossible through the UI, but cheap to
      repair — get one, or their videos would have nowhere to go.

    Runs after ``add_missing_columns``, so ``schedules.name``/``position`` and
    ``video_items.schedule_id`` already exist here.
    """
    done: list[str] = []
    tables = _tables(conn)
    if "schedules" not in tables:
        return done

    conn.execute(text("DROP INDEX IF EXISTS ix_schedules_account_id"))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_schedules_account_id ON schedules (account_id)"
    ))
    done.append("schedules.account_id index no longer unique")

    conn.execute(text(
        "UPDATE schedules SET name = 'Cola principal' WHERE name IS NULL OR name = ''"
    ))
    conn.execute(text("UPDATE schedules SET position = 0 WHERE position IS NULL"))

    orphans = [row[0] for row in conn.execute(text(
        "SELECT id FROM accounts WHERE id NOT IN (SELECT account_id FROM schedules)"
    ))]
    for account_id in orphans:
        conn.execute(text(
            "INSERT INTO schedules (account_id, name, position, enabled, "
            "interval_min_minutes, interval_max_minutes, min_gap_minutes, jitter_seconds, "
            "max_per_day, windows, days_of_week, timezone, to_drafts, privacy_level, "
            "watch_inbox) VALUES (:id, 'Cola principal', 0, 0, 55, 75, 15, 90, 6, '[]', "
            "'[0, 1, 2, 3, 4, 5, 6]', 'Europe/Madrid', 0, 'PUBLIC_TO_EVERYONE', 1)"
        ), {"id": account_id})
    if orphans:
        done.append(f"{len(orphans)} account(s) without a queue got one")

    if "video_items" in tables:
        attached = conn.execute(text(
            "UPDATE video_items SET schedule_id = ("
            "  SELECT id FROM schedules WHERE schedules.account_id = video_items.account_id"
            "  ORDER BY position, id LIMIT 1"
            ") WHERE schedule_id IS NULL"
        )).rowcount
        done.append(f"{attached} video(s) attached to their account's queue")
    return done


# --------------------------------------------------------------------------- #
# v3 -> v4: everything is published directly and publicly
# --------------------------------------------------------------------------- #

def migrate_v3_to_v4(conn) -> list[str]:
    """Drop drafts, privacy levels and Sound Boost.

    All three existed to work around the same thing: TikTok's Business app
    publishes videos publicly and nothing else, so anything other than a public
    post had to detour through the creator's drafts inbox, and a viral sound
    could only be added by hand in TikTok's editor. Neither detour was wanted,
    and the sound one could never do what it promised — no public TikTok API
    attaches a sound from the general library — so the columns and the whole
    ``sounds`` table go with them.

    Videos keep their history: only the columns disappear, never a row.
    """
    done: list[str] = []
    tables = _tables(conn)

    if "schedules" in tables:
        for column in ("to_drafts", "privacy_level", "sound_boost_enabled"):
            action = _drop(conn, "schedules", column)
            if action:
                done.append(action)

    if "video_items" in tables:
        action = _drop(conn, "video_items", "suggested_sound")
        if action:
            done.append(action)

    if "sounds" in tables:
        conn.execute(text("DROP TABLE sounds"))
        done.append("sounds table dropped")

    return done


# --------------------------------------------------------------------------- #
# v4 -> v5: the AI label is on by default, and a queue can be handed to Zernio
# --------------------------------------------------------------------------- #

def migrate_v4_to_v5(conn) -> list[str]:
    """Turn the AI disclosure on for the queues that already existed.

    Until now «Contenido generado por IA» was a checkbox nobody remembered to
    tick. It is now the default of every queue, and each video copies the value
    of its queue when it enters, so a video published months ago keeps whatever
    it went out with. Videos still waiting are given the label their queue now
    carries; the ones already gone are left alone (``is_aigc`` stays NULL and
    means "whatever the queue said back then").

    ``video_items.is_aigc`` and ``video_items.delegated_at`` are added by
    ``add_missing_columns`` before this runs.
    """
    done: list[str] = []
    tables = _tables(conn)

    if "schedules" in tables:
        touched = conn.execute(text(
            "UPDATE schedules SET is_aigc = 1 WHERE is_aigc = 0 OR is_aigc IS NULL"
        )).rowcount
        done.append(f"{touched} queue(s) now label their videos as AI-generated")

    if "video_items" in tables and "is_aigc" in _columns(conn, "video_items"):
        labelled = conn.execute(text(
            "UPDATE video_items SET is_aigc = ("
            "  SELECT is_aigc FROM schedules WHERE schedules.id = video_items.schedule_id"
            ") WHERE is_aigc IS NULL AND schedule_id IS NOT NULL "
            "AND status IN ('QUEUED', 'SCHEDULED')"
        )).rowcount
        done.append(f"{labelled} queued video(s) inherited their queue's AI label")

    return done


# --------------------------------------------------------------------------- #
# v5 -> v6: one minimum separation, on the account
# --------------------------------------------------------------------------- #

def migrate_v5_to_v6(conn) -> list[str]:
    """Move ``schedules.min_gap_minutes`` onto the account and drop it.

    Until now there were two numbers with the same name: a queue's own floor
    under its random interval, and the account-wide distance between two posts
    whatever queue they came from. Only the second was ever shown in the UI,
    and having both let a queue undercut the account. There is one now, on the
    account, and it does both jobs.

    Each account keeps the *largest* of the numbers it had, so no account comes
    out of the upgrade posting closer together than it did before.
    """
    done: list[str] = []
    if "schedules" not in _tables(conn):
        return done
    if "min_gap_minutes" not in _columns(conn, "schedules"):
        return done

    raised = conn.execute(text(
        "UPDATE accounts SET min_gap_minutes = ("
        "  SELECT MAX(s.min_gap_minutes) FROM schedules s WHERE s.account_id = accounts.id"
        ") WHERE ("
        "  SELECT MAX(s.min_gap_minutes) FROM schedules s WHERE s.account_id = accounts.id"
        ") > COALESCE(min_gap_minutes, 0)"
    )).rowcount
    if raised:
        done.append(f"{raised} account(s) took the largest gap their queues had")

    action = _drop(conn, "schedules", "min_gap_minutes")
    if action:
        done.append(action + " (the account's is the only one now)")
    return done


# --------------------------------------------------------------------------- #
# v6 -> v7: publication times a catch-up poll had flattened
# --------------------------------------------------------------------------- #

# Two publications of one TikTok account this close together are not something
# the scheduler can produce - the account's minimum separation is minutes - so
# they are one poll stamping a whole batch with the instant it ran.
_CATCHUP_WINDOW_SECONDS = 120
# Below this the old stamp is near enough to the truth that rewriting it buys
# nothing, and Zernio does legitimately publish a few minutes late.
_CATCHUP_MIN_LATENESS_SECONDS = 600


def migrate_v6_to_v7(conn) -> list[str]:
    """Give back their own hour to videos a single poll stamped all at once.

    ``published_at`` used to be "when this app noticed", which is right for a
    video it watched go up and wrong for a delegated one: Zernio publishes
    those while the app may be closed, so a batch caught up in one tick all
    got the second the app reopened. The analytics credit views to the hour in
    this column, so those posts were skewing the "mejor hora" report.

    Only rows carrying that signature are touched: delegated, published far
    later than the hour they were handed over with, and sharing their instant
    with another post of the same account. A delegated post that was polled
    live, alone, keeps the stamp it has - that one is real.
    """
    done: list[str] = []
    if "video_items" not in _tables(conn):
        return done
    if not {"delegated_at", "published_at", "scheduled_at"} <= _columns(conn, "video_items"):
        return done

    repaired = conn.execute(text(
        "UPDATE video_items SET published_at = scheduled_at WHERE id IN ("
        "  SELECT a.id FROM video_items a WHERE a.status = 'PUBLISHED'"
        "    AND a.delegated_at IS NOT NULL"
        "    AND a.published_at IS NOT NULL AND a.scheduled_at IS NOT NULL"
        "    AND (julianday(a.published_at) - julianday(a.scheduled_at)) * 86400.0 >= :late"
        "    AND EXISTS ("
        "      SELECT 1 FROM video_items b WHERE b.account_id = a.account_id AND b.id <> a.id"
        "        AND b.published_at IS NOT NULL"
        "        AND ABS(julianday(b.published_at) - julianday(a.published_at)) * 86400.0"
        "            <= :window"
        "    )"
        ")"
    ), {"late": _CATCHUP_MIN_LATENESS_SECONDS, "window": _CATCHUP_WINDOW_SECONDS}).rowcount
    if repaired:
        done.append(f"{repaired} delegated video(s) got their real publication hour back")
    return done


# --------------------------------------------------------------------------- #
# v7 -> v8: the library replaces the per-queue watch folder
# --------------------------------------------------------------------------- #

def migrate_v7_to_v8(conn) -> list[str]:
    """Drop ``schedules.watch_inbox``.

    A queue no longer picks files up by itself: every video enters the shared
    library (media/library, which is the watch folder now) and is added to a
    queue from there. ``library_items`` and ``video_items.library_item_id``
    are created by ``create_all``/``add_missing_columns`` before this runs;
    moving the existing files is tools/library_copy.py's job, not a start-up
    step, because it touches tens of gigabytes.
    """
    done: list[str] = []
    if "schedules" in _tables(conn):
        action = _drop(conn, "schedules", "watch_inbox")
        if action:
            done.append(action + " (the library is the watch folder now)")
    return done


# --------------------------------------------------------------------------- #
# v8 -> v9: delegated posts given up on while the app was closed
# --------------------------------------------------------------------------- #

def migrate_v8_to_v9(conn) -> list[str]:
    """Give the delegated videos failed by the 48 h deadline another look.

    Until v9 the deadline was checked before asking Zernio, so reopening the
    app more than 48 h after a post's hour failed it unseen, even though
    Zernio had published it on time. They go back to DELEGATED with their
    post id and the worker asks once: published, failed for real, or still
    pending (and only then failed again).
    """
    if "video_items" not in _tables(conn):
        return []
    revived = conn.execute(text(
        "UPDATE video_items SET status = 'DELEGATED', last_error = NULL, "
        "next_status_check_at = NULL "
        "WHERE status = 'FAILED' AND zernio_post_id IS NOT NULL "
        "AND last_error LIKE 'Zernio no confirmó la publicación%'"
    )).rowcount
    return [f"{revived} delegated video(s) failed by the deadline sent back to be "
            "asked about"] if revived else []


# --------------------------------------------------------------------------- #
# v9 -> v10: the project folder was renamed
# --------------------------------------------------------------------------- #

def migrate_v9_to_v10(conn) -> list[str]:
    """Follow the videos whose saved path went away with the old folder name.

    The project folder was renamed (it is Scheduler now). Paths saved through
    the link that keeps the old name still work; the ones saved through the
    old folder itself point nowhere. Each of those found at the same place
    under this install's media folders is rewritten, in the library and in
    the queues alike.
    """
    tables = _tables(conn)
    moved = 0
    for table in ("library_items", "video_items"):
        if table not in tables:
            continue
        for row_id, path in conn.execute(text(f"SELECT id, file_path FROM {table}")).all():
            new = _rebased(path)
            if new is not None:
                conn.execute(text(f"UPDATE {table} SET file_path = :path WHERE id = :id"),
                             {"path": new, "id": row_id})
                moved += 1
    return [f"{moved} file path(s) pointed at the renamed project folder"] if moved else []


def _rebased(path: str | None) -> str | None:
    """Where ``path`` is now under this install's media folders, if it moved."""
    if not path or Path(path).exists():
        return None
    for folder in _settings.media_dirs:
        marker = f"/media/{folder.name}/"
        if marker in path:
            candidate = Path(folder) / path.split(marker, 1)[1]
            return str(candidate) if candidate.exists() else None
    return None


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #

@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope; commits on success, rolls back on error."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
