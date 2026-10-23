"""Model -> JSON conversion for the UI."""
from __future__ import annotations

from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..models import Account, Comment, EventLog, LibraryItem, Schedule, VideoItem
from ..services import queue


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


# strftime("%a"/"%b") follows the machine's C locale, which is English almost
# everywhere. Spell the Spanish names out so the UI never depends on it.
DAY_ABBR = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")


def local_str(dt: datetime | None, tz_name: str) -> str | None:
    if not dt:
        return None
    return dt.astimezone(ZoneInfo(tz_name)).strftime("%d/%m %H:%M")


def local_long(dt: datetime, tz_name: str) -> str:
    """e.g. 'mar 15/09 09:02' — used in the slot preview."""
    local = dt.astimezone(ZoneInfo(tz_name))
    return f"{DAY_ABBR[local.weekday()]} {local:%d/%m %H:%M}"


def _start_holds(schedule: Schedule, now: datetime | None = None) -> bool:
    """Is «Empezar desde» still in the future (that day at its hour, local to the queue)?"""
    if schedule.start_date is None:
        return False
    start = datetime.combine(schedule.start_date, schedule.start_time or time.min,
                             tzinfo=ZoneInfo(schedule.timezone))
    return start > (now or datetime.now(timezone.utc))


def pending_start(schedule: Schedule, now: datetime | None = None) -> str | None:
    """«Empezar desde» as YYYY-MM-DD while it still holds the queue back, else None."""
    return schedule.start_date.isoformat() if _start_holds(schedule, now) else None


def pending_start_time(schedule: Schedule, now: datetime | None = None) -> str | None:
    """Its hour as HH:MM while it holds the queue back; None for 00:00 or once reached."""
    if not schedule.start_time or not _start_holds(schedule, now):
        return None
    return f"{schedule.start_time:%H:%M}"


def account_json(account: Account) -> dict:
    first = account.schedules[0] if account.schedules else None
    tz = first.timezone if first else "Europe/Madrid"
    return {
        "id": account.id,
        "external_id": account.external_id,
        "zernio_account_id": account.zernio_account_id,
        "username": account.username,
        "display_name": account.display_name,
        "profile_name": account.profile_name,
        "is_active": account.is_active,
        "has_key": bool(account.api_key_enc),
        "platform": account.platform or "tiktok",
        # Ceiling shared by every queue of the account.
        "max_per_day": account.max_per_day,
        "min_gap_minutes": account.min_gap_minutes,
        "queue_count": len(account.schedules),
        "created_at": iso(account.created_at),
        "created_local": local_str(account.created_at, tz),
        "last_checked_at": iso(account.last_checked_at),
        "last_checked_local": local_str(account.last_checked_at, tz),
        "label": account_label(account),
    }


def account_label(account: Account) -> str:
    return account.display_name or (f"@{account.username}" if account.username else
                                    f"Cuenta {account.id}")


def schedule_json(schedule: Schedule | None) -> dict | None:
    """One queue: identity, rules and where it stands right now."""
    if schedule is None:
        return None
    return {
        "id": schedule.id,
        "account_id": schedule.account_id,
        "name": schedule.name,
        "position": schedule.position,
        "enabled": schedule.enabled,
        "interval_min_minutes": schedule.interval_min_minutes,
        "interval_max_minutes": schedule.interval_max_minutes,
        "jitter_seconds": schedule.jitter_seconds,
        "hidden": bool(schedule.hidden),
        "max_per_day": schedule.max_per_day,
        "windows": schedule.windows or [],
        "days_of_week": schedule.days_of_week or list(range(7)),
        "timezone": schedule.timezone,
        "disable_comment": schedule.disable_comment,
        "disable_duet": schedule.disable_duet,
        "disable_stitch": schedule.disable_stitch,
        "is_aigc": schedule.is_aigc,
        "brand_content_toggle": schedule.brand_content_toggle,
        "brand_organic_toggle": schedule.brand_organic_toggle,
        "start_date": pending_start(schedule),
        "start_time": pending_start_time(schedule),
        "next_slot_at": iso(schedule.next_slot_at),
        "next_slot_local": local_str(schedule.next_slot_at, schedule.timezone),
        "last_post_at": iso(schedule.last_post_at),
        "last_post_local": local_str(schedule.last_post_at, schedule.timezone),
    }


# What the queue view shows of a transcript (a tooltip); the editor asks for all of it.
TRANSCRIPT_EXCERPT_CHARS = 280


def video_json(item: VideoItem, tz_name: str = "Europe/Madrid", *,
               comment_count: int | None = None, light: bool = False) -> dict:
    """One queued video as the UI sees it.

    ``light`` is for /api/state, polled every few seconds with every video of
    every account: the transcript (kilobytes each) becomes an excerpt, and the
    caller passes ``comment_count`` counted in one query instead of loading
    every video's comments.
    """
    probe = item.probe or {}
    transcript = item.transcript
    return {
        "id": item.id,
        "account_id": item.account_id,
        "schedule_id": item.schedule_id,
        "queue_name": item.schedule.name if item.schedule is not None else None,
        "original_name": item.original_name,
        "file_path": item.file_path,
        "library_item_id": item.library_item_id,
        "library_folder": item.library_item.folder if item.library_item is not None else None,
        "title": item.title,
        "first_comment": item.first_comment,
        "rendered_title": item.rendered_title,
        "caption": item.caption,
        "rendered_caption": item.rendered_caption,
        "status": item.status.value,
        "priority": item.priority,
        # Turn dealt by the last shuffle; null = arrival order (see RELEASE_ORDER).
        "queue_rank": item.queue_rank,
        # «Ahora» or a retry: it keeps the front, and reordering never moves it.
        "on_its_way": bool(item.force_publish or item.extra or item.attempts),
        # Published «fuera de turno»: on top of the queue's rhythm.
        "extra": bool(item.extra),
        "scheduled_at": iso(item.scheduled_at),
        "scheduled_local": local_str(item.scheduled_at, tz_name),
        # AI label: what this video will actually declare, and whether that is
        # its own decision or still the queue's default.
        "is_aigc": bool(item.is_aigc) if item.is_aigc is not None else None,
        # Delegation: handed to Zernio, and whether its bytes got there.
        "delegated_at": iso(item.delegated_at),
        "delegated_local": local_str(item.delegated_at, tz_name),
        "handed_over": bool(item.zernio_post_id) if item.status.value == "DELEGATED" else None,
        "published_at": iso(item.published_at),
        "published_local": local_str(item.published_at, tz_name),
        # When it entered this account's queues (moving between queues keeps it).
        "created_at": iso(item.created_at),
        "created_local": local_str(item.created_at, tz_name),
        "size_mb": round((item.size_bytes or 0) / (1024 * 1024), 1),
        "duration_sec": round(probe.get("duration_sec", 0) or 0, 1),
        "resolution": (
            f"{probe.get('width')}x{probe.get('height')}" if probe.get("width") else None
        ),
        "fps": round(probe.get("fps", 0) or 0, 1),
        "has_audio": probe.get("has_audio"),
        "attempts": item.attempts,
        "last_error": item.last_error,
        "preflight_errors": item.preflight_errors,
        "post_url": item.post_url,
        "zernio_post_id": item.zernio_post_id,
        "metrics": metric_json(item.metric),
        "comment_count": len(item.comments) if comment_count is None else comment_count,
        # Automatic text: where it stands and where it came from.
        "caption_status": item.caption_status or "none",
        "caption_source": item.caption_source,
        "caption_error": item.caption_error,
        "caption_generated_at": iso(item.caption_generated_at),
        "auto_hashtags": item.auto_hashtags or [],
        **({"transcript_excerpt": transcript[:TRANSCRIPT_EXCERPT_CHARS] if transcript else None,
            "has_transcript": bool(transcript)} if light else {"transcript": transcript}),
    }


def library_json(item: LibraryItem, videos: list[VideoItem] | None = None,
                 tz_name: str = "Europe/Madrid") -> dict:
    """One library video: the file, its texts per platform and where it is queued."""
    from ..services import library

    probe = item.probe or {}
    return {
        "id": item.id,
        "folder": item.folder,
        "original_name": item.original_name,
        "file_path": item.file_path,
        "size_mb": round((item.size_bytes or 0) / (1024 * 1024), 1),
        "duration_sec": round(probe.get("duration_sec", 0) or 0, 1),
        "resolution": (
            f"{probe.get('width')}x{probe.get('height')}" if probe.get("width") else None
        ),
        "has_audio": probe.get("has_audio"),
        # Its cover: YouTube's custom thumbnail and TikTok's cover (GET /api/library/{id}/cover).
        "has_cover": queue.cover_path(Path(item.file_path)) is not None,
        "preflight_errors": item.preflight_errors or [],
        "preflight_warnings": item.preflight_warnings or [],
        "caption_status": item.caption_status or "none",
        "caption_source": item.caption_source,
        "caption_error": item.caption_error,
        "caption_generated_at": iso(item.caption_generated_at),
        "transcript": item.transcript,
        "texts": {platform: library.texts_of(item, platform) for platform in library.PLATFORMS},
        "created_at": iso(item.created_at),
        "created_local": local_str(item.created_at, tz_name),
        # Where it has been queued, one entry per queue.
        "queued": [
            {
                "video_id": v.id,
                "account_id": v.account_id,
                "schedule_id": v.schedule_id,
                "queue_name": v.schedule.name if v.schedule is not None else None,
                "queue_hidden": bool(v.schedule is not None and v.schedule.hidden),
                "platform": library.platform_of(v.account),
                "status": v.status.value,
                "follows_library": v.caption_source == library.FROM_LIBRARY,
                # Its texts can still change what goes out (see queue.writable).
                "writable": v.status.value in ("QUEUED", "SCHEDULED")
                or (v.status.value == "DELEGATED" and not v.zernio_post_id),
                "post_url": v.post_url,
            }
            for v in (videos if videos is not None else item.videos)
        ],
    }


def metric_json(metric) -> dict | None:
    """The latest numbers of a published video, or None if never synced."""
    if metric is None:
        return None
    return {
        "views": metric.views,
        "likes": metric.likes,
        "comments": metric.comments,
        "shares": metric.shares,
        "profile_views": metric.profile_views,
        "completion_rate": round(metric.completion_rate, 4),
        "engagement_rate": round(metric.engagement_rate, 2),
        "fetched_at": iso(metric.fetched_at),
        "platform_updated_at": iso(metric.platform_updated_at),
    }


def comment_json(comment: Comment, tz_name: str = "Europe/Madrid") -> dict:
    # With several accounts connected, the video name alone does not say
    # whose inbox a comment is in.
    account = comment.video.account if comment.video is not None else None
    return {
        "id": comment.id,
        "video_id": comment.video_id,
        "account_id": comment.account_id,
        "account_label": (account_label(account) if account is not None
                          else f"Cuenta {comment.account_id}"),
        "account_username": account.username if account is not None else None,
        "account_platform": (account.platform or "tiktok") if account is not None else "tiktok",
        "video_name": comment.video.original_name if comment.video is not None else None,
        "post_url": comment.video.post_url if comment.video is not None else None,
        "external_id": comment.external_id,
        "parent_external_id": comment.parent_external_id,
        "author": comment.author_username or comment.author_name or "—",
        "message": comment.message,
        "like_count": comment.like_count,
        "reply_count": comment.reply_count,
        "is_owner": comment.is_owner,
        "hidden": comment.hidden,
        "answered": comment.answered,
        "created_at": iso(comment.created_at),
        "created_local": local_str(comment.created_at, tz_name),
    }


def event_json(event: EventLog, tz_name: str = "Europe/Madrid") -> dict:
    return {
        "id": event.id,
        "ts": iso(event.ts),
        "ts_local": event.ts.astimezone(ZoneInfo(tz_name)).strftime("%d/%m %H:%M:%S"),
        "level": event.level,
        "kind": event.kind,
        "message": event.message,
        "account_id": event.account_id,
        "video_id": event.video_id,
        "data": event.data,
    }
