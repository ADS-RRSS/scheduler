"""Request/response models for the JSON API."""
from __future__ import annotations

from datetime import date, datetime, time

from pydantic import BaseModel, Field, field_validator

from .scheduler.planner import MINUTES_PER_DAY


class WindowIn(BaseModel):
    start: int = Field(ge=0, le=MINUTES_PER_DAY - 1)
    end: int = Field(ge=1, le=MINUTES_PER_DAY)


class ScheduleIn(BaseModel):
    """The full configuration of one queue, as the form sends it."""

    # Optional: when present the queue is renamed as part of the save.
    name: str | None = Field(default=None, max_length=64)
    enabled: bool = False
    interval_min_minutes: int = Field(ge=1, le=7 * 24 * 60)
    interval_max_minutes: int = Field(ge=1, le=7 * 24 * 60)
    jitter_seconds: int = Field(default=90, ge=0, le=3600)
    max_per_day: int | None = Field(default=None, ge=0, le=200)
    windows: list[WindowIn] = Field(default_factory=list)
    days_of_week: list[int] = Field(default_factory=lambda: list(range(7)))
    timezone: str = "Europe/Madrid"
    # «Empezar desde»: None => now; at start_time local to the queue (None => 00:00).
    start_date: date | None = None
    start_time: time | None = None
    disable_comment: bool = False
    disable_duet: bool = False
    disable_stitch: bool = False
    is_aigc: bool = False
    brand_content_toggle: bool = False
    brand_organic_toggle: bool = False

    @field_validator("interval_max_minutes")
    @classmethod
    def _check_interval(cls, v: int, info):
        lo = info.data.get("interval_min_minutes")
        if lo is not None and v < lo:
            raise ValueError("el intervalo máximo debe ser mayor o igual que el mínimo")
        return v

    @field_validator("start_time")
    @classmethod
    def _check_start_time(cls, v: time | None, info):
        # Minutes only, and nothing without a day.
        if v is None or info.data.get("start_date") is None:
            return None
        return v.replace(second=0, microsecond=0, tzinfo=None)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str | None):
        if v is None:
            return None
        name = v.strip()
        if not name:
            raise ValueError("la cola necesita un nombre")
        return name

    @field_validator("days_of_week")
    @classmethod
    def _check_days(cls, v: list[int]):
        if not v:
            raise ValueError("selecciona al menos un día de la semana")
        if any(d not in range(7) for d in v):
            raise ValueError("días inválidos (0=lunes … 6=domingo)")
        return sorted(set(v))


class VideoPatch(BaseModel):
    title: str | None = Field(default=None, max_length=150)
    caption: str | None = Field(default=None, max_length=2200)
    first_comment: str | None = Field(default=None, max_length=500)
    priority: int | None = Field(default=None, ge=0, le=10000)
    # Move the video to another queue, of this account or of another one.
    schedule_id: int | None = None
    # Replace the video's own hashtags ("#tag" or "tag", any separator).
    hashtags: list[str] | None = Field(default=None, max_length=30)
    # TikTok's AI-generated content label for this one video. Sent as null to
    # forget the override and follow the queue again, so "absent" and "null"
    # must stay distinct: the route reads model_fields_set.
    is_aigc: bool | None = None


class LibraryTextsIn(BaseModel):
    """The four texts of one platform; a field left out is not touched."""

    title: str | None = Field(default=None, max_length=150)
    description: str | None = Field(default=None, max_length=5000)
    tags: list[str] | None = Field(default=None, max_length=30)
    first_comment: str | None = Field(default=None, max_length=500)


class LibraryPatch(BaseModel):
    tiktok: LibraryTextsIn | None = None
    youtube: LibraryTextsIn | None = None


class TextCheckIn(BaseModel):
    """The four texts of one platform as the editor holds them, not yet saved."""

    platform: str = Field(default="tiktok", pattern="^(tiktok|youtube)$")
    title: str = Field(default="", max_length=1000)
    description: str = Field(default="", max_length=20_000)
    tags: list[str] = Field(default_factory=list, max_length=100)
    first_comment: str = Field(default="", max_length=5000)
    # What is being edited, for the context: its series, what the video says.
    library_id: int | None = None
    video_id: int | None = None


class TextSuggestIn(BaseModel):
    """«Proponer con IA»: a fresh draft from the video's script or transcript."""

    platform: str = Field(default="tiktok", pattern="^(tiktok|youtube)$")
    library_id: int | None = None
    video_id: int | None = None
    # What the editor holds: the model works from it when the video has no
    # sheet and no transcript (a text written by hand).
    title: str = Field(default="", max_length=1000)
    description: str = Field(default="", max_length=20_000)


class LibraryTagsIn(BaseModel):
    """Hashtags edited on many library videos at once."""

    ids: list[int] = Field(default_factory=list, max_length=2000)
    # Every video of a folder, instead of (or on top of) the ids.
    folder: str | None = Field(default=None, max_length=160)
    platforms: list[str] = Field(default_factory=lambda: ["tiktok", "youtube"], min_length=1)
    add: list[str] = Field(default_factory=list, max_length=10)
    remove: list[str] = Field(default_factory=list, max_length=30)
    # Where the added ones go: first counts for sure (TikTok counts five).
    position: str = Field(default="start", pattern="^(start|end)$")

    @field_validator("platforms")
    @classmethod
    def _check_platforms(cls, v: list[str]):
        bad = [p for p in v if p not in ("tiktok", "youtube")]
        if bad:
            raise ValueError(f"plataformas desconocidas: {', '.join(bad)}")
        return list(dict.fromkeys(v))


class LibraryBulkIn(BaseModel):
    # rewrite: Ollama writes them again; sheet: the texts of the .txt sheet, as
    # they come; delete: out of the library.
    action: str = Field(pattern="^(rewrite|sheet|delete)$")
    ids: list[int] = Field(min_length=1, max_length=2000)
    # A file left in media/library would be registered again on the next
    # tick (it is the watch folder), so removing a video removes its file.
    delete_files: bool = True


class LibraryAiIn(BaseModel):
    """The library switch «Reescribir los textos con IA»."""
    enabled: bool


class FolderRenameIn(BaseModel):
    old: str = Field(min_length=1, max_length=160)
    new: str = Field(min_length=1, max_length=160)


class LibraryMoveIn(BaseModel):
    """Videos of the library into another folder (an existing one or a new name)."""
    ids: list[int] = Field(min_length=1, max_length=2000)
    folder: str = Field(min_length=1, max_length=160)


class FolderLabelsIn(BaseModel):
    folder: str = Field(min_length=1, max_length=160)
    labels: list[str] = Field(default_factory=list, max_length=30)


class LabelColorIn(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    # #rrggbb, or null to go back to the automatic one.
    color: str | None = Field(default=None, max_length=7)


class EnqueueIn(BaseModel):
    """Take videos from the library into a queue."""

    library_ids: list[int] = Field(default_factory=list, max_length=2000)
    # Every video of a library folder, instead of (or on top of) the ids.
    folder: str | None = Field(default=None, max_length=160)
    # Write this queue its own texts instead of using the library's.
    rewrite: bool = False
    # Publish them right away «fuera de turno»: on top of the queue's rhythm,
    # which keeps its hours. They stay rows of the queue (no duplicates later).
    publish_now: bool = False
    # Or hand them to Zernio for this instant, also «fuera de turno». An
    # explicit offset is required: a naive time would mean the server's zone.
    publish_at: datetime | None = None

    @field_validator("publish_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("publish_at necesita zona horaria (p. ej. 2026-10-18T18:30:00+02:00)")
        return value


class DelegateIn(BaseModel):
    """Hand a queue over to Zernio with every hour already calculated."""

    # Only the first N videos of the queue; null = the whole queue.
    limit: int | None = Field(default=None, ge=1, le=500)
    # Only the videos of this library folder; null = every folder.
    folder: str | None = Field(default=None, max_length=160)
    # Or several: the folders ticked (none = all) narrowed by the labels ticked,
    # carrying all of them (``match="all"``) or any (``"any"``).
    label: str | None = Field(default=None, max_length=64)
    folders: list[str] | None = Field(default=None, max_length=500)
    labels: list[str] | None = Field(default=None, max_length=30)
    match: str = Field(default="all", pattern="^(all|any)$")


class VideoIdsIn(BaseModel):
    """Some videos of one queue, ticked in the queue view."""

    video_ids: list[int] = Field(min_length=1, max_length=5000)


class UndelegateIn(BaseModel):
    """Which delegated videos of a queue to take back from Zernio (null = all)."""

    folder: str | None = Field(default=None, max_length=160)
    label: str | None = Field(default=None, max_length=64)
    folders: list[str] | None = Field(default=None, max_length=500)
    labels: list[str] | None = Field(default=None, max_length=30)
    match: str = Field(default="all", pattern="^(all|any)$")
    video_ids: list[int] | None = Field(default=None, max_length=5000)


class ScheduleCreateIn(BaseModel):
    """Add a queue to an account."""

    name: str = Field(default="", max_length=64)
    # Copy the hours, cadence and publishing options of an existing queue.
    copy_from: int | None = None


class SchedulePatch(BaseModel):
    """Small edits that do not need the whole form."""

    name: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None
    hidden: bool | None = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str | None):
        if v is None:
            return None
        name = v.strip()
        if not name:
            raise ValueError("la cola necesita un nombre")
        return name


class QueueOrderIn(BaseModel):
    order: list[int] = Field(min_length=1, max_length=50)


class VideoReorderIn(BaseModel):
    """Waiting videos of a queue moved by hand: to a turn, or next to another video."""
    ids: list[int] = Field(min_length=1, max_length=5000)
    position: int | None = Field(default=None, ge=1)
    before: int | None = None
    after: int | None = None


class PauseIn(BaseModel):
    paused: bool


class GlobalSettingsIn(BaseModel):
    hashtags: str = ""
    webhook_url: str = ""


class CommentReplyIn(BaseModel):
    message: str = Field(min_length=1, max_length=150)

    @field_validator("message")
    @classmethod
    def _strip(cls, v: str) -> str:
        text = v.strip()
        if not text:
            raise ValueError("la respuesta no puede estar vacía")
        return text


class CommentHideIn(BaseModel):
    hidden: bool = True


class CommentAnsweredIn(BaseModel):
    answered: bool = True


# --------------------------------------------------------------------------- #
# Accounts (Zernio)
# --------------------------------------------------------------------------- #

class ZernioKeyIn(BaseModel):
    api_key: str = Field(min_length=8, max_length=512)

    @field_validator("api_key")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class AccountsCreateIn(ZernioKeyIn):
    # Zernio account ids to connect with this key (one row each).
    account_ids: list[str] = Field(min_length=1, max_length=50)
    # Optional display names by Zernio account id.
    labels: dict[str, str] = Field(default_factory=dict)


class AccountPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    is_active: bool | None = None
    # Limits shared by every queue of the account. ``max_per_day`` is sent as
    # null for "sin máximo", so "not sent" and "no limit" must stay distinct:
    # the route only touches the fields present in the request body.
    max_per_day: int | None = Field(default=None, ge=0, le=200)
    # The one separation setting: it holds inside every queue and between them.
    min_gap_minutes: int | None = Field(default=None, ge=0, le=24 * 60)


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #

class PruneEventsIn(BaseModel):
    keep: int = Field(default=500, ge=0, le=100_000)


class ClearFailedIn(BaseModel):
    account_id: int | None = None
    delete_files: bool = False


class WipeAnswerIn(BaseModel):
    """One of the three confirmations of the «borrar todo» button."""

    token: str = Field(min_length=8, max_length=64)
    total: int
    word: str = Field(max_length=64)


class WipeIn(BaseModel):
    token: str = Field(min_length=8, max_length=64)


class DbRowUpdate(BaseModel):
    values: dict[str, object]


class SqlIn(BaseModel):
    sql: str = Field(min_length=1, max_length=20_000)
    limit: int = Field(default=200, ge=1, le=1000)
