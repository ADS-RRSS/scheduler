"""SQLAlchemy models. One SQLite file; schema changes are applied by db.migrate()."""
from __future__ import annotations

import enum
from datetime import date, datetime, time, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .dbtypes import UTCDateTime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class VideoStatus(str, enum.Enum):
    QUEUED = "QUEUED"          # waiting in the queue, no slot yet
    SCHEDULED = "SCHEDULED"    # has a scheduled_at in the future
    UPLOADING = "UPLOADING"    # bytes travelling to Zernio
    PROCESSING = "PROCESSING"  # accepted by Zernio, waiting for TikTok's URL
    # Handed over to Zernio with a future scheduledFor: it publishes on its
    # own, so this app may be closed. Until ``zernio_post_id`` is set the
    # video is still waiting its turn to be uploaded.
    DELEGATED = "DELEGATED"
    PUBLISHED = "PUBLISHED"    # live on TikTok
    FAILED = "FAILED"          # gave up after max attempts


class Account(Base):
    """A TikTok account connected through Zernio.

    One Zernio API key can cover several TikTok accounts; each one is a row here
    with its own queues. The key is stored encrypted (Fernet).

    An account owns one or more :class:`Schedule` rows — "colas" in the UI. Each
    one is an independent queue: its own videos, its own hours and its own
    cadence (e.g. one queue for 10:00-22:00 and another for 22:00-10:00). The
    fields below are the account-wide ceiling that no queue may push past,
    because TikTok counts posts per account, not per queue.
    """

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    # "zernio:<zernio account id>": stable identity across reconnects.
    external_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    zernio_account_id: Mapped[str] = mapped_column(String(128))
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # The Zernio "profile" (workspace folder) the account belongs to.
    profile_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    platform: Mapped[str] = mapped_column(String(32), default="tiktok", server_default="tiktok")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Ceiling shared by every queue of the account. None => unlimited.
    max_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True, default=15)
    # Minimum distance between two posts of this account, whatever queue they
    # come from: two queues never fire back to back, and it is also the floor
    # under the random interval inside each queue.
    min_gap_minutes: Mapped[int] = mapped_column(Integer, default=15)

    # Last time «Probar conexión» confirmed the key still works for this account.
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    schedules: Mapped[list["Schedule"]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
        order_by="Schedule.position, Schedule.id",
    )
    videos: Mapped[list["VideoItem"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    @property
    def default_schedule(self) -> "Schedule | None":
        """The queue that receives videos when none was named explicitly."""
        return self.schedules[0] if self.schedules else None


class Schedule(Base):
    """One queue of an account: its videos plus the rules that release them.

    Several rows may share an ``account_id``; each is shown in the UI as a
    "cola" with its own name, hours, cadence and publishing options.
    """

    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )

    name: Mapped[str] = mapped_column(String(64), default="Cola principal")
    # Display order, and the order the worker walks the queues in.
    position: Mapped[int] = mapped_column(Integer, default=0)

    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # Out of sight: not offered in the library nor among the queue tabs. It
    # says nothing about publishing — a hidden queue that is active still
    # publishes; «enabled» is the switch for that.
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    # Randomised gap between consecutive posts. The hard floor under the random
    # draw is not here but on the account (``Account.min_gap_minutes``): the
    # same number has to hold between two queues, so there is only one of it.
    interval_min_minutes: Mapped[int] = mapped_column(Integer, default=55)
    interval_max_minutes: Mapped[int] = mapped_column(Integer, default=75)
    # Seconds of forward noise so posts never land on a round number.
    jitter_seconds: Mapped[int] = mapped_column(Integer, default=90)

    # None => unlimited.
    max_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True, default=6)

    # [] => "indiferente" (any hour). Each item: {"start": minutes, "end": minutes}
    windows: Mapped[list] = mapped_column(JSON, default=list)
    # 0 = Monday ... 6 = Sunday
    days_of_week: Mapped[list] = mapped_column(JSON, default=lambda: [0, 1, 2, 3, 4, 5, 6])
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Madrid")

    # Every video is published directly and publicly: TikTok's Business app,
    # which is the lane Zernio connects through, does not publish videos with
    # any other privacy, and the drafts detour was removed in v3.
    disable_comment: Mapped[bool] = mapped_column(Boolean, default=False)
    disable_duet: Mapped[bool] = mapped_column(Boolean, default=False)
    disable_stitch: Mapped[bool] = mapped_column(Boolean, default=False)
    # Default AI label for every video entering this queue: each one is born
    # with this value copied onto it (VideoItem.is_aigc) and can be flipped on
    # its own afterwards. On by default because the content this tool queues is
    # generated, and TikTok wants the disclosure on the video, not on the text.
    is_aigc: Mapped[bool] = mapped_column(Boolean, default=True)
    brand_content_toggle: Mapped[bool] = mapped_column(Boolean, default=False)
    brand_organic_toggle: Mapped[bool] = mapped_column(Boolean, default=False)

    # «Empezar desde»: no slot is booked before this local day (in ``timezone``)
    # at ``start_time`` (None => 00:00). None => from now. An instant already
    # reached means nothing and is cleared the next time the queue is planned.
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    start_time: Mapped[time | None] = mapped_column(Time, nullable=True)

    # Next planned posting instant (UTC). Recomputed after each post / config change.
    next_slot_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_post_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    account: Mapped[Account] = relationship(back_populates="schedules")
    videos: Mapped[list["VideoItem"]] = relationship(back_populates="schedule")


class LibraryItem(Base):
    """One video of the shared library, and the texts it goes out with.

    Every file lives here once, in ``media/library/<folder>/``, whatever
    number of queues publish it: a reel sent to TikTok and to YouTube is one
    file on disk and one row here, with two rows in ``video_items`` pointing
    at it. The folder is the series slug of the file name
    (``reel_07_<series>_<topic>`` -> ``<series>``).

    The texts are written here, once, when the video enters the library, for
    each platform separately: a queue copies the ones of its account's
    platform and publishes them as they are (see services/library.py).
    """

    __tablename__ = "library_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    folder: Mapped[str] = mapped_column(String(160), index=True, default="")
    file_path: Mapped[str] = mapped_column(Text)
    original_name: Mapped[str] = mapped_column(Text)
    # One row per content: the same bytes under another name are a duplicate.
    checksum: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    probe: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # A file that fails validation stays listed (so the user sees why) but
    # cannot be added to a queue.
    preflight_errors: Mapped[list | None] = mapped_column(JSON, nullable=True)
    preflight_warnings: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Automatic texts, as on VideoItem: none -> pending -> generating ->
    # done | failed. "none" means nothing is generated (a hand-written .txt,
    # or the feature is off).
    caption_status: Mapped[str] = mapped_column(String(16), default="none", index=True)
    caption_source: Mapped[str | None] = mapped_column(String(24), nullable=True)
    caption_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    caption_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    caption_generated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # 1 = asked for by hand on this one video («Reescribir»): it jumps ahead of
    # a folder being written in bulk, which may be hours of work.
    caption_priority: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The same four texts for each platform. Hashtags/tags are "#tag"
    # strings; the publisher adds the fixed ones from Ajustes and turns them
    # into YouTube's tag list.
    tiktok_title: Mapped[str] = mapped_column(Text, default="", server_default="")
    tiktok_description: Mapped[str] = mapped_column(Text, default="", server_default="")
    tiktok_tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tiktok_first_comment: Mapped[str] = mapped_column(Text, default="", server_default="")
    youtube_title: Mapped[str] = mapped_column(Text, default="", server_default="")
    youtube_description: Mapped[str] = mapped_column(Text, default="", server_default="")
    youtube_tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    youtube_first_comment: Mapped[str] = mapped_column(Text, default="", server_default="")

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    videos: Mapped[list["VideoItem"]] = relationship(back_populates="library_item")


class VideoItem(Base):
    __tablename__ = "video_items"
    __table_args__ = (UniqueConstraint("account_id", "checksum", name="uq_account_checksum"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    # Which queue of that account releases this video. NULL only on history
    # rows whose queue was deleted; a pending item always has one.
    schedule_id: Mapped[int | None] = mapped_column(
        ForeignKey("schedules.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The library video this entry publishes. Its texts were copied from
    # there; while ``caption_source`` is "biblioteca" they keep following it.
    # NULL on rows from before the library, until tools/library_finalize.py.
    library_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_items.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # The library's file (shared with every other queue that publishes it).
    file_path: Mapped[str] = mapped_column(Text)
    original_name: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)

    title: Mapped[str] = mapped_column(Text, default="", server_default="")
    rendered_title: Mapped[str | None] = mapped_column(Text, nullable=True)

    caption: Mapped[str] = mapped_column(Text, default="")
    first_comment: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Caption actually sent (after template rendering) - kept for the history view.
    rendered_caption: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Automatic text. ``caption_status`` walks none -> pending -> generating ->
    # done | failed; "none" means the user wrote the text (or the feature is
    # off) and nothing is generated. The publisher waits for pending/generating
    # up to ``caption_max_wait_minutes``.
    caption_status: Mapped[str] = mapped_column(String(16), default="none", index=True)
    # manual | whisper | whisper+ollama | ficha | ficha+ollama | fallback, or
    # "biblioteca": copied from the library item, and still following it.
    caption_source: Mapped[str | None] = mapped_column(String(24), nullable=True)
    caption_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    caption_generated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # When it went "pending"; the publisher's wait is measured from here.
    caption_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # 1 = «Regenerar» on this video: ahead of the library's bulk work.
    caption_priority: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # What Whisper heard, kept so the text can be rewritten without re-listening.
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Hashtags chosen for this video ("#tag" strings), merged with the fixed
    # ones from Ajustes when the caption is rendered. Editable in the queue.
    auto_hashtags: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # AI-generated content disclosure for this video alone. NULL means "not
    # decided yet": the queue's own ``is_aigc`` answers for it. Set at ingest
    # from the queue, so the queue's later changes never rewrite history.
    is_aigc: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    status: Mapped[VideoStatus] = mapped_column(
        Enum(VideoStatus), default=VideoStatus.QUEUED, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=100)  # lower = sooner
    # Its turn inside the queue, after ``priority``: «Mezclar» numbers the
    # waiting videos 1, 2, 3… in a random order. NULL = never shuffled, which
    # sorts after every numbered video and, among its peers, by arrival (id),
    # so whatever enters after a shuffle joins the end of the queue.
    queue_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Set by "publicar ahora": bypasses the slot and the enabled flag, once.
    force_publish: Mapped[bool] = mapped_column(Boolean, default=False)
    # Published «fuera de turno»: on top of the queue's rhythm, not in it. It
    # never restarts the cadence, fills a day of the budget or keeps the
    # account's separation from the other posts; it stays a row of the queue
    # so the same video can never enter it (or the account) again.
    extra: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)

    zernio_post_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    post_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When the whole queue was handed to Zernio at once: the instant the batch
    # was sent, kept so the UI can group and explain a delegation.
    delegated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    probe: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # ffprobe summary
    preflight_errors: Mapped[list | None] = mapped_column(JSON, nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_status_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    upload_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # Last time the comments of this video were pulled from Zernio.
    comments_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    account: Mapped[Account] = relationship(back_populates="videos")
    schedule: Mapped["Schedule | None"] = relationship(back_populates="videos")
    library_item: Mapped["LibraryItem | None"] = relationship(back_populates="videos")
    metric: Mapped["VideoMetric | None"] = relationship(
        back_populates="video", cascade="all, delete-orphan", uselist=False
    )
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )


class VideoMetric(Base):
    """Latest analytics snapshot of one published video, as Zernio reports it.

    One row per video, overwritten on every sync: this is a scheduler, not a
    time-series database, and "how did this video do" only needs the last
    numbers. ``published_at`` lives on :class:`VideoItem`, which is what the
    per-hour and per-weekday breakdowns group by.
    """

    __tablename__ = "video_metrics"

    video_id: Mapped[int] = mapped_column(
        ForeignKey("video_items.id", ondelete="CASCADE"), primary_key=True
    )

    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    # Business-app connections only; 0 everywhere else (see docs/ANALYTICS.md).
    profile_views: Mapped[int] = mapped_column(Integer, default=0)
    completion_rate: Mapped[float] = mapped_column(Float, default=0.0)
    # As Zernio computes it, in percent. Recomputed locally when absent.
    engagement_rate: Mapped[float] = mapped_column(Float, default=0.0)

    # When Zernio last refreshed the numbers with TikTok, and when we read them.
    platform_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    video: Mapped["VideoItem"] = relationship(back_populates="metric")


class Comment(Base):
    """A TikTok comment on one of our published videos.

    Mirrored locally so the queue view can show them offline and so a reply is
    only sent once. ``external_id`` is TikTok's own comment id.
    """

    __tablename__ = "comments"
    __table_args__ = (
        UniqueConstraint("video_id", "external_id", name="uq_comment_video_external"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("video_items.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )

    external_id: Mapped[str] = mapped_column(String(128), index=True)
    # Set on a reply; TikTok returns replies as separate comments with a parent.
    parent_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    author_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    author_username: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    like_count: Mapped[int] = mapped_column(Integer, default=0)
    reply_count: Mapped[int] = mapped_column(Integer, default=0)
    # Written by the connected account itself, so it needs no answer.
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    # We have answered this one from here.
    answered: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    video: Mapped["VideoItem"] = relationship(back_populates="comments")


class EventLog(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(16), default="info")  # info|warn|error|success
    kind: Mapped[str] = mapped_column(String(48), default="generic")
    message: Mapped[str] = mapped_column(Text)
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    video_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Setting(Base):
    """Small global key/value store (pause switch, hashtags, schema version, ...)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
