"""The JSON API consumed by the UI: state, accounts, schedules, queue, analytics."""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import APP_VERSION, get_settings
from ..db import get_session
from ..models import Account, Comment, LibraryItem, Schedule, Setting, VideoItem, VideoStatus
from ..scheduler import captioner as captioner_mod
from ..scheduler import worker as worker_mod
from ..scheduler.planner import INTERVAL_PRESETS, WINDOW_PRESETS
from ..schemas import (
    AccountPatch,
    AccountsCreateIn,
    CommentAnsweredIn,
    CommentHideIn,
    CommentReplyIn,
    DelegateIn,
    GlobalSettingsIn,
    PauseIn,
    QueueOrderIn,
    ScheduleCreateIn,
    ScheduleIn,
    SchedulePatch,
    UndelegateIn,
    VideoIdsIn,
    VideoPatch,
    VideoReorderIn,
    ZernioKeyIn,
)
from ..services import accounts as accounts_svc
from ..services import analytics as analytics_svc
from ..services import autocaption
from ..services import comments as comments_svc
from ..services import delegation as delegation_svc
from ..services import hashtags as hashtags_svc
from ..services import captions, events, library, media, notify, publisher, queue, scheduling
from ..services.zernio import ZernioClient, ZernioError
from .serializers import (
    account_json,
    comment_json,
    event_json,
    iso,
    local_long,
    local_str,
    schedule_json,
    video_json,
)

router = APIRouter(prefix="/api", tags=["api"])


def _account_or_404(session: Session, account_id: int) -> Account:
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(404, "Cuenta no encontrada")
    return account


def _video_or_404(session: Session, video_id: int) -> VideoItem:
    item = session.get(VideoItem, video_id)
    if item is None:
        raise HTTPException(404, "Vídeo no encontrado")
    return item


def _schedule_or_404(session: Session, schedule_id: int) -> Schedule:
    schedule = session.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(404, "Cola no encontrada")
    return schedule


def _queue_of(item: VideoItem) -> Schedule:
    """The queue a video belongs to, or its account's first one."""
    schedule = item.schedule or item.account.default_schedule
    if schedule is None:
        raise HTTPException(409, "El vídeo no está en ninguna cola.")
    return schedule


def _get_setting(session: Session, key: str, default: str = "") -> str:
    row = session.get(Setting, key)
    return (row.value or {}).get("value", default) if row else default


def _set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row is None:
        row = Setting(key=key)
        session.add(row)
    row.value = {"value": value}


def _media_path_or_403(item: VideoItem) -> Path:
    """Only files inside the library (or the old media folders) may be served."""
    settings = get_settings()
    path = Path(item.file_path).resolve()
    allowed = [Path(d).resolve() for d in settings.media_dirs]
    if not any(path.is_relative_to(root) for root in allowed):
        raise HTTPException(403, "El archivo está fuera de las carpetas de la aplicación.")
    if not path.exists():
        raise HTTPException(404, "El archivo ya no existe.")
    return path


def _zernio_http_error(exc: ZernioError) -> HTTPException:
    status = 400 if exc.http_status in (400, 401, 402, 403, 404) else 502
    return HTTPException(status, exc.message)


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #

# Below this much free space the UI warns: an upload of the library lands on
# this disk, and SQLite fails badly when it cannot grow its journal.
LOW_DISK_BYTES = 10 * 1024 ** 3
LOW_DISK_SHARE = 0.05


def disk_status(settings) -> dict:
    """Free space where the library lives."""
    try:
        usage = shutil.disk_usage(str(settings.media_library_dir))
    except OSError:
        return {"free": None, "total": None, "low": False}
    low = usage.free < max(LOW_DISK_BYTES, usage.total * LOW_DISK_SHARE) if usage.total else False
    return {"free": usage.free, "total": usage.total, "low": bool(low)}


@router.get("/health")
def health(session: Session = Depends(get_session)) -> dict:
    settings = get_settings()
    worker = worker_mod.worker
    return {
        "ok": True,
        "version": APP_VERSION,
        "base_url": settings.base_url,
        "ffmpeg": media.ffprobe_available(),
        "scheduler_running": worker.running,
        "scheduler_last_tick": iso(worker.last_tick_at),
        "scheduler_error": worker.last_error,
        "paused": worker_mod.is_paused(session),
        "accounts": len(accounts_svc.list_all(session)),
        "timezone": settings.default_timezone,
        "webhook_configured": bool(notify.resolve_url(session, settings)),
        "zernio_api_base": settings.zernio_api_base,
        "passcode_enabled": bool(settings.ui_passcode),
        "ai": ai_status(),
        "disk": disk_status(settings),
    }


def ai_status() -> dict:
    """Whisper and Ollama, as the Ajustes page and the queue show them."""
    settings = get_settings()
    whisper_ok, whisper_error = autocaption.whisper_available()
    ollama = autocaption.probe_ollama(settings) if settings.caption_ai_enabled else None
    worker = captioner_mod.captioner
    return {
        "enabled": settings.caption_ai_enabled,
        "running": worker.running,
        "current_video_id": worker.current_video_id,
        "processed": worker.processed,
        "whisper": {
            "available": whisper_ok,
            "error": whisper_error,
            "model": settings.whisper_model,
            "language": settings.whisper_language or "auto",
            "cached": autocaption.model_cached(settings),
        },
        "ollama": {
            "url": settings.ollama_url,
            "reachable": bool(ollama and ollama.reachable),
            "model": ollama.model if ollama else None,
            "models": list(ollama.models) if ollama else [],
            "error": ollama.error if ollama else "",
        },
        # What actually goes out: TikTok counts five, whatever .env says.
        "hashtag_max": min(settings.hashtag_max, hashtags_svc.TIKTOK_MAX_TAGS),
    }


@router.get("/presets")
def presets() -> dict:
    return {
        "intervals": INTERVAL_PRESETS,
        "windows": WINDOW_PRESETS,
        "days": ["L", "M", "X", "J", "V", "S", "D"],
    }


def _stats(counts: dict[str, int], today: int) -> dict:
    """The numbers of one queue (or of a whole account), from queue.status_counts."""
    def c(*statuses: VideoStatus) -> int:
        return sum(counts.get(s.value, 0) for s in statuses)

    return {
        "queued": c(VideoStatus.QUEUED, VideoStatus.SCHEDULED),
        "in_flight": c(VideoStatus.UPLOADING, VideoStatus.PROCESSING),
        # Waiting in Zernio, and how many of those have not been uploaded yet:
        # that difference is the progress bar of a delegation in flight.
        "delegated": c(VideoStatus.DELEGATED),
        "handover_pending": counts.get("handover", 0),
        "published": c(VideoStatus.PUBLISHED),
        "failed": c(VideoStatus.FAILED),
        "today": today,
    }


def _sum_counts(per_queue: dict) -> dict[str, int]:
    total: dict[str, int] = {}
    for counts in per_queue.values():
        for key, value in counts.items():
            total[key] = total.get(key, 0) + value
    return total


def _caption_queue(session: Session) -> dict:
    """How many texts are waiting to be written, and how long that is at today's pace."""
    library_waiting = session.scalar(
        select(func.count()).select_from(LibraryItem).where(
            LibraryItem.caption_status.in_(library.WRITING))
    ) or 0
    own_waiting = session.scalar(
        select(func.count()).select_from(VideoItem).where(
            VideoItem.caption_status.in_(("pending", "generating")),
            captioner_mod.writable(), captioner_mod.own_text(),
        )
    ) or 0
    each = captioner_mod.captioner.seconds_per_text()
    waiting = library_waiting + own_waiting
    return {
        "waiting": waiting,
        "seconds_each": each,
        "eta_seconds": round(waiting * each) if each is not None and waiting else None,
    }


@router.get("/state")
def state(session: Session = Depends(get_session)) -> dict:
    settings = get_settings()
    accounts = accounts_svc.list_all(session)
    payload = []

    for account in accounts:
        items = queue.for_state(session, account.id, settings.state_history_limit)
        counts = queue.status_counts(session, account.id)
        comments = dict(session.execute(
            select(Comment.video_id, func.count())
            .where(Comment.account_id == account.id)
            .group_by(Comment.video_id)
        ).all())
        default = account.default_schedule
        account_tz_name = default.timezone if default else settings.default_timezone
        account_tz = ZoneInfo(account_tz_name)

        queues = []
        for schedule in account.schedules:
            tz = ZoneInfo(schedule.timezone)
            queues.append(
                {
                    "schedule": schedule_json(schedule),
                    "warnings": scheduling.warnings_for(schedule),
                    "stats": _stats(
                        counts.get(schedule.id, {}),
                        queue.committed_on(
                            session, datetime.now(tz).date(), tz, schedule_id=schedule.id
                        ),
                    ),
                }
            )

        totals = _sum_counts(counts)
        history_total = sum(totals.get(s.value, 0) for s in queue.HISTORY_STATUSES)
        history_shown = sum(1 for i in items if i.status in queue.HISTORY_STATUSES)
        tz_by_queue = {s.id: s.timezone for s in account.schedules}
        payload.append(
            {
                "account": account_json(account),
                "queues": queues,
                "warnings": scheduling.account_warnings(account),
                "stats": _stats(
                    totals,
                    queue.committed_on(
                        session, datetime.now(account_tz).date(), account_tz,
                        account_id=account.id,
                    ),
                ),
                # Published and failed videos beyond the latest ones are not
                # listed (the history only grows); every other one always is.
                "history": {"shown": history_shown, "total": history_total,
                            "limit": settings.state_history_limit},
                "videos": [
                    video_json(i, tz_by_queue.get(i.schedule_id, account_tz_name),
                               comment_count=comments.get(i.id, 0), light=True)
                    for i in items
                ],
            }
        )

    captions_queue = _caption_queue(session)
    return {
        "health": health(session),
        "accounts": payload,
        "events": [event_json(e) for e in events.recent(session, 120)],
        "comments_pending": comments_svc.pending_count(session),
        "captions_pending": session.scalar(
            select(func.count()).select_from(VideoItem).where(
                VideoItem.caption_status.in_(("pending", "generating")),
                captioner_mod.writable(), captioner_mod.own_text(),
            )
        ) or 0,
        "captions": captions_queue,
        "library": {
            "total": session.scalar(select(func.count()).select_from(LibraryItem)) or 0,
            "writing": session.scalar(
                select(func.count()).select_from(LibraryItem).where(
                    LibraryItem.caption_status.in_(library.WRITING))
            ) or 0,
            # The Cola page's folder filter offers these labels and colours its folders.
            "folder_labels": library.folder_labels(session),
            "label_colors": library.label_colors(session),
        },
        "settings": {
            "hashtags": _get_setting(session, "hashtags"),
            "webhook_url": _get_setting(session, "webhook_url", settings.webhook_url),
        },
        "server_time": iso(datetime.now(timezone.utc)),
    }


@router.post("/system/pause")
def pause(body: PauseIn, session: Session = Depends(get_session)) -> dict:
    worker_mod.set_paused(session, body.paused)
    events.record(
        session, "system",
        "Programación pausada." if body.paused else "Programación reanudada.",
        level="warn" if body.paused else "success",
    )
    session.commit()
    return {"paused": body.paused}


@router.get("/settings")
def read_settings(session: Session = Depends(get_session)) -> dict:
    settings = get_settings()
    return {
        "hashtags": _get_setting(session, "hashtags"),
        "webhook_url": _get_setting(session, "webhook_url", settings.webhook_url),
    }


@router.put("/settings")
def write_settings(body: GlobalSettingsIn, session: Session = Depends(get_session)) -> dict:
    _set_setting(session, "hashtags", " ".join(captions.parse_hashtags(body.hashtags)))
    _set_setting(session, "webhook_url", body.webhook_url.strip())
    session.commit()
    return read_settings(session)


@router.post("/settings/webhook-test")
async def test_webhook(session: Session = Depends(get_session)) -> dict:
    url = notify.resolve_url(session, get_settings())
    if not url:
        raise HTTPException(400, "No hay ningún webhook configurado.")
    ok = await notify.send(
        url, "Prueba de avisos ✅", "Si lees esto, los avisos de Scheduler funcionan.",
        {"hora": datetime.now().strftime("%H:%M")},
    )
    if not ok:
        raise HTTPException(502, "El webhook no aceptó el mensaje. Revisa la URL.")
    return {"ok": True, "kind": notify.prepare(url, "", "").kind}


@router.get("/events")
def list_events(
    level: str | None = None, account_id: int | None = None, kind: str | None = None,
    q: str | None = None, limit: int = 200, session: Session = Depends(get_session),
) -> dict:
    rows = events.search(session, level=level or None, account_id=account_id,
                         kind=kind or None, q=q or None, limit=limit)
    return {"events": [event_json(e) for e in rows]}


# --------------------------------------------------------------------------- #
# Accounts (Zernio)
# --------------------------------------------------------------------------- #

@router.post("/zernio/accounts")
async def zernio_accounts(body: ZernioKeyIn, session: Session = Depends(get_session)) -> dict:
    """Validate a Zernio API key and list the TikTok accounts it can publish to."""
    settings = get_settings()
    try:
        found = await ZernioClient(body.api_key, settings.zernio_api_base).list_accounts("")
    except ZernioError as exc:
        raise _zernio_http_error(exc) from exc
    except Exception as exc:  # noqa: BLE001 - shown to the user
        raise HTTPException(502, f"No se pudo contactar con Zernio: {exc}") from exc
    found = [a for a in found if a.get("platform") in ("tiktok", "youtube")]
    connected = {a.zernio_account_id for a in accounts_svc.list_all(session)}
    for acc in found:
        acc["already_connected"] = acc["id"] in connected
    return {"accounts": found}


@router.post("/accounts")
async def create_accounts(body: AccountsCreateIn, session: Session = Depends(get_session)) -> dict:
    """Connect one or several TikTok accounts with the same Zernio key."""
    settings = get_settings()
    try:
        found = await ZernioClient(body.api_key, settings.zernio_api_base).list_accounts("")
    except ZernioError as exc:
        raise _zernio_http_error(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"No se pudo contactar con Zernio: {exc}") from exc

    by_id = {a["id"]: a for a in found}
    added, skipped = [], []
    for wanted in body.account_ids:
        match = by_id.get(wanted.strip())
        if match is None:
            skipped.append({"id": wanted, "reason": "no aparece en tu Zernio"})
            continue
        try:
            account = accounts_svc.create(
                session, api_key=body.api_key, zernio_account=match,
                label=body.labels.get(wanted, ""),
            )
        except ValueError as exc:
            skipped.append({"id": wanted, "reason": str(exc)})
            continue
        added.append(account_json(account))
    if not added and skipped:
        session.rollback()
        raise HTTPException(409, "; ".join(f"{s['id']}: {s['reason']}" for s in skipped))
    session.commit()
    return {"added": added, "skipped": skipped}


@router.patch("/accounts/{account_id}")
def patch_account(
    account_id: int, body: AccountPatch, session: Session = Depends(get_session)
) -> dict:
    account = _account_or_404(session, account_id)
    sent = body.model_fields_set
    if body.display_name is not None:
        account.display_name = body.display_name.strip() or None
    if body.is_active is not None and body.is_active != account.is_active:
        account.is_active = body.is_active
        events.record(session, "account_toggled",
                      f"Cuenta {'activada' if body.is_active else 'desactivada'}: "
                      f"{account.display_name}", level="info", account_id=account.id)

    # "max_per_day": null means "sin máximo", so only an explicitly sent field
    # counts as a change.
    limits_changed = False
    if "max_per_day" in sent and body.max_per_day != account.max_per_day:
        account.max_per_day = body.max_per_day
        limits_changed = True
    if "min_gap_minutes" in sent and body.min_gap_minutes is not None \
            and body.min_gap_minutes != account.min_gap_minutes:
        account.min_gap_minutes = body.min_gap_minutes
        limits_changed = True
    if limits_changed:
        session.flush()
        scheduling.replan_account(session, account)
        events.record(
            session, "account_limits_updated",
            "Límites de la cuenta: "
            + (f"{account.max_per_day} vídeos/día" if account.max_per_day is not None
               else "sin máximo diario")
            + f" · {account.min_gap_minutes} min entre publicaciones.",
            account_id=account.id,
        )
    session.commit()
    return account_json(account)


@router.get("/accounts/{account_id}/key")
def reveal_key(account_id: int, session: Session = Depends(get_session)) -> dict:
    """Return the stored Zernio API key. Logged, because it is a secret."""
    account = _account_or_404(session, account_id)
    key = accounts_svc.api_key(account)
    if not key:
        raise HTTPException(404, "Esta cuenta no tiene API key guardada.")
    events.record(session, "account_key_revealed",
                  f"API key de Zernio mostrada para {account.display_name}.",
                  level="warn", account_id=account.id)
    session.commit()
    return {"api_key": key}


@router.put("/accounts/{account_id}/key")
async def replace_key(
    account_id: int, body: ZernioKeyIn, session: Session = Depends(get_session)
) -> dict:
    """Swap the API key after validating that it still covers this account."""
    account = _account_or_404(session, account_id)
    settings = get_settings()
    try:
        found = await ZernioClient(body.api_key, settings.zernio_api_base).list_accounts(account.platform)
    except ZernioError as exc:
        raise _zernio_http_error(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"No se pudo contactar con Zernio: {exc}") from exc
    if not any(a["id"] == account.zernio_account_id for a in found):
        raise HTTPException(400, "Esa API key es válida pero no da acceso a esta cuenta.")
    accounts_svc.set_api_key(session, account, body.api_key)
    session.commit()
    return account_json(account)


@router.post("/accounts/{account_id}/test")
async def test_account(account_id: int, session: Session = Depends(get_session)) -> dict:
    account = _account_or_404(session, account_id)
    try:
        result = await accounts_svc.verify(session, account, get_settings())
    except ZernioError as exc:
        session.commit()
        raise _zernio_http_error(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"No se pudo contactar con Zernio: {exc}") from exc
    session.commit()
    result["account_json"] = account_json(account)
    return result


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, session: Session = Depends(get_session)) -> dict:
    account = _account_or_404(session, account_id)
    accounts_svc.disconnect(session, account)
    session.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Queues ("colas": one Schedule row each)
# --------------------------------------------------------------------------- #

def _queue_payload(session: Session, schedule: Schedule) -> dict:
    return {
        "schedule": schedule_json(schedule),
        "warnings": scheduling.warnings_for(schedule),
    }


@router.post("/accounts/{account_id}/schedules")
def create_schedule(
    account_id: int, body: ScheduleCreateIn, session: Session = Depends(get_session)
) -> dict:
    """Add a queue to an account, optionally copying another one's rules."""
    account = _account_or_404(session, account_id)
    copy_from = None
    if body.copy_from is not None:
        copy_from = _schedule_or_404(session, body.copy_from)
        if copy_from.account_id != account.id:
            raise HTTPException(400, "Esa cola es de otra cuenta.")
    try:
        schedule = scheduling.create_schedule(
            session, account, name=body.name, copy_from=copy_from
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()
    return _queue_payload(session, schedule)


@router.put("/accounts/{account_id}/schedules/order")
def reorder_schedules(
    account_id: int, body: QueueOrderIn, session: Session = Depends(get_session)
) -> dict:
    account = _account_or_404(session, account_id)
    known = {s.id for s in account.schedules}
    if not set(body.order) <= known:
        raise HTTPException(400, "Hay colas de otra cuenta en el orden enviado.")
    scheduling.reorder(session, account, body.order)
    session.commit()
    session.refresh(account)
    return {"schedules": [schedule_json(s) for s in account.schedules]}


@router.put("/schedules/{schedule_id}")
def update_schedule(
    schedule_id: int, body: ScheduleIn, session: Session = Depends(get_session)
) -> dict:
    """Save the whole configuration of one queue."""
    schedule = _schedule_or_404(session, schedule_id)
    start_moved = (body.start_date, body.start_time) != (schedule.start_date, schedule.start_time)
    values = body.model_dump(exclude={"windows", "name"})
    if body.name is not None:
        values["name"] = scheduling.next_name_for(schedule, body.name)
    for field, value in values.items():
        setattr(schedule, field, value)
    schedule.windows = [w.model_dump() for w in body.windows]

    try:
        scheduling.policy_from_schedule(schedule)
    except Exception as exc:  # noqa: BLE001 - validation feedback
        session.rollback()
        raise HTTPException(400, str(exc)) from exc

    scheduling.invalidate(session, schedule)
    scheduling.plan_next_slot(session, schedule)
    events.record(
        session, "schedule_updated",
        f"Programación guardada en «{schedule.name}»"
        + (f" · próximo hueco {local_str(schedule.next_slot_at, schedule.timezone)}"
           if schedule.next_slot_at else " · sin huecos válidos"),
        account_id=schedule.account_id,
    )
    payload = _queue_payload(session, schedule)
    parked = queue.delegated(session, schedule.account_id, schedule.id) if start_moved else []
    if parked:
        # Those already carry their hour in Zernio; only a new handover moves them.
        note = (f"{len(parked)} vídeo(s) de «{schedule.name}» ya están programados en Zernio "
                "y no cambian de fecha: recupéralos y vuelve a enviarlos para aplicar el nuevo "
                "«Empezar desde».")
        events.record(session, "schedule_start_delegated", note, level="warn",
                      account_id=schedule.account_id)
        payload["warnings"] = [*payload["warnings"], note]
    session.commit()
    return payload


@router.patch("/schedules/{schedule_id}")
def patch_schedule(
    schedule_id: int, body: SchedulePatch, session: Session = Depends(get_session)
) -> dict:
    """Rename a queue, start/stop it, or hide/show it."""
    schedule = _schedule_or_404(session, schedule_id)
    if body.name is not None and body.name != schedule.name:
        schedule.name = scheduling.next_name_for(schedule, body.name)
        events.record(session, "queue_renamed", f"Cola renombrada: «{schedule.name}»",
                      account_id=schedule.account_id)
    if body.enabled is not None and body.enabled != schedule.enabled:
        schedule.enabled = body.enabled
        events.record(
            session, "schedule_toggled",
            f"Cola «{schedule.name}» {'activada' if body.enabled else 'parada'}.",
            account_id=schedule.account_id,
        )
        scheduling.invalidate(session, schedule)
    if body.hidden is not None and body.hidden != bool(schedule.hidden):
        schedule.hidden = body.hidden
        events.record(
            session, "queue_hidden",
            f"Cola «{schedule.name}» {'oculta' if body.hidden else 'visible de nuevo'}.",
            account_id=schedule.account_id,
        )
    scheduling.plan_next_slot(session, schedule)
    session.commit()
    return _queue_payload(session, schedule)


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: int,
    move_to: int | None = None,
    purge: bool = False,
    delete_files: bool = False,
    force: bool = False,
    session: Session = Depends(get_session),
) -> dict:
    """Remove a queue.

    ``move_to`` rehomes its videos in another queue; ``purge`` deletes them with
    it, and ``delete_files`` also removes the files from disk. A queue holding
    videos needs one of the two, so nothing is ever dropped by accident.

    ``force`` purges a queue even with videos delegated to Zernio: their posts
    are cancelled there first, as far as Zernio lets, and the rest is reported
    as still in Zernio rather than stopping the deletion.
    """
    schedule = _schedule_or_404(session, schedule_id)
    target = _schedule_or_404(session, move_to) if move_to is not None else None
    if target is not None and purge:
        raise HTTPException(400, "Elige una cosa: mover los vídeos o borrarlos.")
    name, account_id = schedule.name, schedule.account_id
    withdrawn = {"cancelled": 0, "left": 0, "problem": ""}
    parked = queue.delegated(session, account_id, schedule.id)
    # Only when the deletion is certain to go ahead: cancelling in Zernio and
    # then refusing to delete would leave rows pointing at nothing.
    if force and purge and parked and not scheduling.blockers(session, schedule):
        withdrawn = await delegation_svc.withdraw(schedule.account, parked, get_settings())
    try:
        result = scheduling.delete_schedule(
            session, schedule, target, purge=purge, delete_files=delete_files, force=force
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if withdrawn["left"]:
        events.record(
            session, "delegation_orphaned",
            f"{withdrawn['left']} post(s) de la cola borrada «{name}» siguen en Zernio y se "
            f"publicarán: Zernio no dejó cancelarlos ({withdrawn['problem']}). Bórralos "
            "desde Zernio si no quieres que salgan.",
            level="error", account_id=account_id,
        )
    session.commit()
    return {"ok": True, **result,
            "cancelled_in_zernio": withdrawn["cancelled"],
            "left_in_zernio": withdrawn["left"],
            "zernio_problem": withdrawn["problem"]}


@router.get("/schedules/{schedule_id}/preview")
def preview_slots(
    schedule_id: int, count: int = 10, session: Session = Depends(get_session)
) -> dict:
    schedule = _schedule_or_404(session, schedule_id)
    count = max(1, min(count, 50))
    slots = scheduling.preview(session, schedule, count)
    tz = ZoneInfo(schedule.timezone)
    return {
        "timezone": schedule.timezone,
        "warnings": scheduling.warnings_for(schedule),
        "slots": [
            {
                "utc": iso(s),
                "local": local_long(s, schedule.timezone),
                "date": s.astimezone(tz).strftime("%Y-%m-%d"),
            }
            for s in slots
        ],
    }


# --------------------------------------------------------------------------- #
# Order: «Mezclar», and back to the order of arrival
# --------------------------------------------------------------------------- #

def _replan_head(session: Session, schedule: Schedule) -> None:
    """The order changed: the old head gives its slot up to the new one."""
    scheduling.invalidate(session, schedule)
    scheduling.plan_next_slot(session, schedule)


@router.post("/schedules/{schedule_id}/shuffle")
def shuffle_queue(schedule_id: int, session: Session = Depends(get_session)) -> dict:
    """Deal the queue's waiting videos a new random order, as often as wanted.

    The queue publishes in that order, and a delegation hands the videos to
    Zernio in that order too: both walk the queue as it stands.
    """
    schedule = _schedule_or_404(session, schedule_id)
    held, mixed = queue.shuffle(session, schedule.id)
    if not mixed:
        raise HTTPException(
            400, f"«{schedule.name}» necesita al menos dos vídeos esperando su turno para "
                 "poder mezclarlos.")
    _replan_head(session, schedule)
    if not held:
        where = f"sale primero «{mixed[0].original_name}»"
    elif len(held) == 1:
        where = f"detrás de «{held[0].original_name}», que ya está saliendo"
    else:
        where = f"detrás de los {len(held)} que ya están saliendo"
    events.record(
        session, "queue_shuffled",
        f"Cola «{schedule.name}» mezclada: {len(mixed)} vídeo(s) en un orden nuevo, {where}.",
        account_id=schedule.account_id,
    )
    session.commit()
    return {
        "shuffled": len(mixed),
        "held": len(held),
        "order": [item.id for item in held + mixed],
        "message": f"{len(mixed)} vídeos mezclados, {where}.",
    }


@router.post("/schedules/{schedule_id}/reorder")
def reorder_queue(schedule_id: int, body: VideoReorderIn,
                  session: Session = Depends(get_session)) -> dict:
    """Move waiting videos by hand: to a turn, or before or after another video.

    Like a shuffle, the queue publishes in the new order and a delegation hands
    the videos to Zernio in it.
    """
    schedule = _schedule_or_404(session, schedule_id)
    try:
        order, moved = queue.reorder(session, schedule.id, body.ids, position=body.position,
                                     before=body.before, after=body.after)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _replan_head(session, schedule)
    first = order.index(moved[0]) + 1
    if len(moved) == 1:
        what = f"«{moved[0].original_name}» pasa al turno {first} de {len(order)}"
    else:
        what = f"{len(moved)} vídeos pasan a los turnos {first}–{first + len(moved) - 1} de {len(order)}"
    events.record(session, "queue_reordered", f"Cola «{schedule.name}»: {what}.",
                  account_id=schedule.account_id)
    session.commit()
    return {"order": [item.id for item in order], "moved": len(moved), "first": first,
            "message": what + "."}


@router.post("/schedules/{schedule_id}/unshuffle")
def unshuffle_queue(schedule_id: int, session: Session = Depends(get_session)) -> dict:
    """Undo the shuffles: the waiting videos go back to the order they arrived in."""
    schedule = _schedule_or_404(session, schedule_id)
    reset = queue.unshuffle(session, schedule.id)
    _replan_head(session, schedule)
    if reset:
        events.record(
            session, "queue_unshuffled",
            f"Cola «{schedule.name}» de vuelta al orden de llegada ({reset} vídeo(s)).",
            account_id=schedule.account_id,
        )
    session.commit()
    return {
        "reset": reset,
        "message": (f"«{schedule.name}» vuelve al orden de llegada." if reset
                    else f"«{schedule.name}» ya estaba en el orden de llegada."),
    }


# --------------------------------------------------------------------------- #
# Delegation: hand a whole queue to Zernio, hours included
# --------------------------------------------------------------------------- #

def _scope(session: Session, folder: str | None = None, label: str | None = None,
           folders: list[str] | None = None, labels: list[str] | None = None,
           match: str = "all") -> delegation_svc.Scope:
    """What a queue's bulk action works on: one folder, a combination, or everything.

    The folders ticked (none = all) narrowed by the labels ticked; ``folder`` and
    ``label`` are the one-of-each shorthand.
    """
    folders = list(dict.fromkeys([*(folders or []), *([folder] if folder else [])]))
    labels = list(dict.fromkeys(
        x.strip().lower() for x in [*(labels or []), *([label] if label else [])] if x and x.strip()))
    if not labels and len(folders) == 1:
        return folders[0]
    names = library.resolve_folders(session, folders, labels, match)
    if names is None:
        return None
    return delegation_svc.FolderSet(frozenset(names), delegation_svc.describe(folders, labels, match))


def _delegation_payload(session: Session, schedule: Schedule, planned: list,
                        folder: delegation_svc.Scope = None) -> dict:
    """What the confirmation dialog shows before anything is committed."""
    tz = ZoneInfo(schedule.timezone)
    gaps = delegation_svc.spacing_minutes(planned)
    everything = queue.pending(session, schedule.id)
    pending = delegation_svc.in_folder(everything, folder)
    writing = [
        entry for entry in planned
        if entry.item.caption_status in ("pending", "generating")
    ]
    days = sorted({entry.when.astimezone(tz).strftime("%Y-%m-%d") for entry in planned})

    warnings = list(scheduling.warnings_for(schedule))
    if folder is not None and len(everything) > len(pending):
        warnings.append(
            f"Sólo se {'envían' if isinstance(folder, delegation_svc.FolderSet) else 'envía'} "
            f"{delegation_svc.scope_text(folder)}. "
            f"Los otros {len(everything) - len(pending)} "
            f"vídeo(s) de «{schedule.name}» siguen en la cola y empiezan cuando Zernio haya "
            "publicado estos, con la misma separación."
        )
    if len(planned) < len(pending):
        warnings.append(
            f"{len(pending) - len(planned)} vídeo(s) se quedan en la cola: las reglas de "
            f"«{schedule.name}» no dan más huecos (revisa el máximo diario, las franjas "
            "y los días activos)."
        )
    if writing:
        warnings.append(
            f"{len(writing)} vídeo(s) siguen escribiendo su texto automático. Se envían "
            f"igual: se espera hasta {get_settings().caption_max_wait_minutes} min por cada "
            "uno y, si no llega, sale con el texto que tenga."
        )
    if worker_mod.is_paused(session):
        warnings.append(
            "La programación está pausada, así que los vídeos no empezarán a subirse a "
            "Zernio hasta que la reanudes."
        )
    if not schedule.account.is_active:
        warnings.append("La cuenta está desactivada: actívala en Cuentas antes de enviar.")

    return {
        "queue": schedule_json(schedule),
        "folder": folder if isinstance(folder, str) else None,
        "folders": sorted(folder.folders) if isinstance(folder, delegation_svc.FolderSet) else None,
        "count": len(planned),
        "pending": len(pending),
        "left_behind": len(pending) - len(planned),
        "days": len(days),
        "gap_min": min(gaps) if gaps else None,
        "gap_max": max(gaps) if gaps else None,
        "gap_avg": round(sum(gaps) / len(gaps)) if gaps else None,
        "first_local": local_long(planned[0].when, schedule.timezone) if planned else None,
        "last_local": local_long(planned[-1].when, schedule.timezone) if planned else None,
        "already_delegated": len(queue.delegated(session, schedule.account_id, schedule.id)),
        "warnings": warnings,
        "posts": [
            {
                "video_id": entry.item.id,
                "name": entry.item.original_name,
                "utc": iso(entry.when),
                "local": local_long(entry.when, schedule.timezone),
                "date": entry.when.astimezone(tz).strftime("%Y-%m-%d"),
                "gap_minutes": gaps[i - 1] if i else None,
                "caption_pending": entry.item.caption_status in ("pending", "generating"),
                "is_aigc": publisher.aigc_for(entry.item, schedule),
            }
            for i, entry in enumerate(planned)
        ],
    }


@router.get("/schedules/{schedule_id}/delegation")
def delegation_preview(
    schedule_id: int, limit: int | None = None, folder: str | None = None,
    label: str | None = None, folders: list[str] | None = Query(None),
    labels: list[str] | None = Query(None), match: str = Query("all", pattern="^(all|any)$"),
    session: Session = Depends(get_session),
) -> dict:
    """The exact hour every video of the queue (or of one folder) would get in Zernio.

    ``folders`` and ``labels`` combine (see :func:`_scope`) as if they were one folder.

    Pure simulation: nothing is sent and nothing changes until the POST below.
    """
    schedule = _schedule_or_404(session, schedule_id)
    scope = _scope(session, folder, label, folders, labels, match)
    planned = delegation_svc.plan(session, schedule, limit=limit, folder=scope)
    return _delegation_payload(session, schedule, planned, scope)


@router.post("/schedules/{schedule_id}/delegate")
def delegate_queue(
    schedule_id: int, body: DelegateIn, session: Session = Depends(get_session)
) -> dict:
    """Hand the queue to Zernio: it publishes it, this app may be closed.

    The videos leave the local rotation here; their bytes travel one by one
    afterwards, which the queue view reports as it happens.
    """
    schedule = _schedule_or_404(session, schedule_id)
    account = schedule.account
    if not account.is_active:
        raise HTTPException(409, "La cuenta está desactivada: actívala en Cuentas.")
    if not account.api_key_enc:
        raise HTTPException(409, "Esta cuenta no tiene clave de Zernio guardada.")
    scope = _scope(session, body.folder, body.label, body.folders, body.labels, body.match)
    if not delegation_svc.in_folder(queue.pending(session, schedule.id), scope):
        raise HTTPException(400, f"«{schedule.name}» no tiene vídeos pendientes"
                                 + (f" de {delegation_svc.scope_text(scope)}." if scope else "."))

    result = delegation_svc.delegate(session, schedule, limit=body.limit, folder=scope)
    if not result["delegated"]:
        session.rollback()
        raise HTTPException(
            409,
            f"Las reglas de «{schedule.name}» no dan ningún hueco válido: revisa el máximo "
            "diario, las franjas y los días de la semana.",
        )
    session.commit()
    return {
        "delegated": result["delegated"],
        "left_behind": result["left_behind"],
        "first_local": local_long(result["first"], schedule.timezone),
        "last_local": local_long(result["last"], schedule.timezone),
        "message": (
            f"{result['delegated']} vídeo(s) programados en Zernio, del "
            f"{local_long(result['first'], schedule.timezone)} al "
            f"{local_long(result['last'], schedule.timezone)}. Se van subiendo de uno en uno; "
            "cuando termine la subida puedes cerrar la aplicación."
        ),
    }


@router.post("/schedules/{schedule_id}/delegation/cancel")
async def cancel_delegation(
    schedule_id: int, body: UndelegateIn | None = None,
    session: Session = Depends(get_session),
) -> dict:
    """Bring a delegated queue back: cancels in Zernio and re-queues locally.

    With a body, only one folder's videos or the ones selected in the queue.
    """
    schedule = _schedule_or_404(session, schedule_id)
    body = body or UndelegateIn()
    result = await delegation_svc.cancel_batch(
        session, schedule.account, get_settings(), schedule=schedule,
        folder=_scope(session, body.folder, body.label, body.folders, body.labels, body.match),
        video_ids=set(body.video_ids) if body.video_ids is not None else None,
    )
    session.commit()
    if result["problem"] and not result["recovered"] and not result["published"]:
        raise HTTPException(502, result["problem"])
    return {
        **result,
        "message": f"{result['recovered']} vídeo(s) han vuelto a la cola."
                   + (f" {result['published']} ya estaban publicados en Zernio: quedan "
                      "como publicados." if result["published"] else "")
                   + (f" Zernio falló con el resto: {result['problem']}"
                      if result["problem"] else ""),
    }


@router.post("/videos/{video_id}/undelegate")
async def undelegate_video(video_id: int, session: Session = Depends(get_session)) -> dict:
    """Take one video back from Zernio, leaving the rest of the batch alone."""
    item = _video_or_404(session, video_id)
    if item.status != VideoStatus.DELEGATED:
        raise HTTPException(409, "Este vídeo no está programado en Zernio.")
    try:
        back = await delegation_svc.cancel(session, item.account, item, get_settings())
    except ZernioError as exc:
        raise _zernio_http_error(exc) from exc
    schedule = _queue_of(item)
    if back:
        scheduling.plan_next_slot(session, schedule)
        events.record(session, "delegation_cancelled",
                      f"«{item.original_name}» recuperado de Zernio: vuelve a la cola.",
                      level="warn", account_id=item.account_id, video_id=item.id)
    else:
        # Zernio had already published it: marked as such, nothing came back.
        scheduling.replan_account(session, item.account)
    session.commit()
    return video_json(item, schedule.timezone)


# --------------------------------------------------------------------------- #
# Queue
# --------------------------------------------------------------------------- #

@router.get("/videos/{video_id}")
def get_video(video_id: int, session: Session = Depends(get_session)) -> dict:
    """One video with everything, transcript included (the editor reads it)."""
    item = _video_or_404(session, video_id)
    return video_json(item, _queue_of(item).timezone)


@router.patch("/videos/{video_id}")
def patch_video(
    video_id: int, body: VideoPatch, session: Session = Depends(get_session)
) -> dict:
    item = _video_or_404(session, video_id)
    previous = item.schedule
    if "is_aigc" in body.model_fields_set:
        # null = follow the queue again; True/False = this video decides.
        item.is_aigc = body.is_aigc
        events.record(
            session, "aigc_changed",
            f"«{item.original_name}»: "
            + ("marcado como generado por IA" if body.is_aigc
               else "ya no se marca como generado por IA" if body.is_aigc is False
               else "vuelve a seguir la etiqueta de IA de su cola"),
            account_id=item.account_id, video_id=item.id,
        )
    if item.caption_source == library.FROM_LIBRARY and {
            "title", "caption", "first_comment", "hashtags"} & body.model_fields_set:
        # Edited here: this queue keeps its own texts from now on, and the
        # library's later changes no longer overwrite them.
        item.caption_source = "manual"
    if body.title is not None:
        item.title = body.title
    if body.first_comment is not None:
        item.first_comment = body.first_comment
    if body.caption is not None:
        item.caption = body.caption
        # The human's words are final: anything being generated is dropped.
        if item.caption_status in ("pending", "generating"):
            item.caption_status = "none"
        item.caption_source = "manual"
        item.caption_error = None
    if body.hashtags is not None:
        item.auto_hashtags = hashtags_svc.normalize_all(body.hashtags)
    if body.priority is not None:
        item.priority = body.priority
    if body.schedule_id is not None and body.schedule_id != item.schedule_id:
        if item.status in (VideoStatus.UPLOADING, VideoStatus.PROCESSING):
            raise HTTPException(409, "No se puede mover un vídeo que se está subiendo.")
        target = _schedule_or_404(session, body.schedule_id)
        if target.account_id != item.account_id:
            # Another TikTok account: the per-account duplicate rule applies.
            duplicate = session.query(VideoItem).filter(
                VideoItem.account_id == target.account_id,
                VideoItem.checksum == item.checksum,
            ).first()
            if duplicate is not None:
                raise HTTPException(
                    409, f"{target.account.display_name} ya tiene ese vídeo en una cola."
                )
        item.account_id = target.account_id
        item.schedule_id = target.id
        if item.library_item is not None and item.caption_source == library.FROM_LIBRARY:
            # Another platform may want other texts: the library has them.
            library.apply_to_video(item, item.library_item, library.platform_of(target.account))
        # Its turn came from the other queue's shuffle: here it goes by
        # arrival, behind whatever the target's own shuffle placed.
        item.queue_rank = None
        if item.status == VideoStatus.SCHEDULED:
            item.status = VideoStatus.QUEUED
            item.scheduled_at = None
        events.record(session, "video_moved",
                      f"«{item.original_name}» movido a la cola «{target.name}» "
                      f"de {target.account.display_name}.",
                      account_id=target.account_id, video_id=item.id)
        session.flush()
        if previous is not None:
            scheduling.plan_next_slot(session, previous)
        scheduling.plan_next_slot(session, target)
    elif body.priority is not None and item.schedule is not None:
        scheduling.plan_next_slot(session, item.schedule)
    session.commit()
    session.refresh(item)
    return video_json(item, _queue_of(item).timezone)


@router.delete("/videos/{video_id}")
async def delete_video(
    video_id: int, delete_file: bool = False, session: Session = Depends(get_session)
) -> dict:
    item = _video_or_404(session, video_id)
    settings = get_settings()
    if item.status == VideoStatus.UPLOADING:
        # The bytes may be leaving right now. Only an upload older than any
        # real one (the app was closed half-way) is stuck and can be dropped.
        limit = settings.status_timeout_minutes
        started = (item.upload_started_at or item.created_at).astimezone(timezone.utc)
        if datetime.now(timezone.utc) - started < timedelta(minutes=limit):
            raise HTTPException(
                409, f"Se está subiendo ahora mismo. Si en {limit} min sigue igual, "
                "podrás quitarlo.")
    if item.status in (VideoStatus.DELEGATED, VideoStatus.PROCESSING):
        # Deleting the row would not stop Zernio: the post has to go first.
        # A post Zernio no longer has (404) is no obstacle: that is how a
        # «procesando» that will never finish gets unstuck.
        try:
            cancelled = await delegation_svc.cancel(session, item.account, item, settings,
                                                    back_to_queue=False)
        except ZernioError as exc:
            raise _zernio_http_error(exc) from exc
        if not cancelled:
            # Zernio had already published it: it stays, as history.
            session.commit()
            return {"ok": True, "published": True}
    account_id = item.account_id
    schedule = item.schedule
    name = item.original_name
    # A library file is shared with the other queues: it is deleted from the
    # library page, never from here. Only a file from before the library goes.
    if delete_file and not library.owns(item.file_path):
        Path(item.file_path).unlink(missing_ok=True)
        # Or the next video uploaded with that name would inherit its text.
        queue.remove_sidecars(Path(item.file_path))
    session.delete(item)
    session.flush()
    if schedule is not None:
        scheduling.plan_next_slot(session, schedule)
    events.record(
        session, "video_removed", f"Vídeo eliminado de la cola: «{name}»",
        level="warn", account_id=account_id,
    )
    session.commit()
    return {"ok": True}


@router.post("/schedules/{schedule_id}/videos/remove")
def remove_pending(
    schedule_id: int, body: VideoIdsIn, session: Session = Depends(get_session)
) -> dict:
    """Take several waiting videos out of a queue at once.

    Only what is still local (queued or with its slot) leaves: a video in
    Zernio is taken back first («Recuperar»), one uploading is left alone. The
    files stay in the library, where the other queues may still need them.
    """
    schedule = _schedule_or_404(session, schedule_id)
    wanted = set(body.video_ids)
    items = list(session.scalars(select(VideoItem).where(
        VideoItem.id.in_(wanted), VideoItem.schedule_id == schedule.id)))
    removable = [i for i in items if i.status in (VideoStatus.QUEUED, VideoStatus.SCHEDULED)]
    skipped = len(wanted) - len(removable)
    for item in removable:
        session.delete(item)
    session.flush()
    if removable:
        scheduling.plan_next_slot(session, schedule)
        names = [i.original_name for i in removable]
        events.record(
            session, "video_removed",
            f"{len(removable)} vídeo(s) quitados de la cola «{schedule.name}»: "
            + ", ".join(f"«{n}»" for n in names[:5])
            + (f" y {len(names) - 5} más" if len(names) > 5 else ""),
            level="warn", account_id=schedule.account_id,
        )
    session.commit()
    return {
        "removed": len(removable),
        "skipped": skipped,
        "message": f"{len(removable)} vídeo(s) quitados de «{schedule.name}»"
                   + (f" · {skipped} no estaban esperando y se quedan" if skipped else "")
                   + ".",
    }


@router.post("/videos/{video_id}/publish-now")
def publish_now(video_id: int, session: Session = Depends(get_session)) -> dict:
    item = _video_or_404(session, video_id)
    if item.status == VideoStatus.DELEGATED:
        raise HTTPException(
            409, "Este vídeo ya está programado en Zernio. Recupéralo primero si quieres "
                 "publicarlo ahora.")
    if item.status not in (VideoStatus.QUEUED, VideoStatus.SCHEDULED, VideoStatus.FAILED):
        raise HTTPException(409, f"El vídeo está en estado {item.status.value}.")
    if item.preflight_errors:
        raise HTTPException(400, "El vídeo no pasa la validación: " + " ".join(item.preflight_errors))
    if not Path(item.file_path).exists():
        raise HTTPException(400, "El archivo ya no existe en disco.")
    if not item.account.is_active:
        raise HTTPException(409, "La cuenta está desactivada: actívala en Cuentas.")

    queue.send_now(item)
    events.record(
        session, "publish_now", f"Publicación inmediata solicitada: «{item.original_name}»",
        account_id=item.account_id, video_id=item.id,
    )
    session.commit()
    if item.caption_status in ("pending", "generating"):
        return {"ok": True, "message": "Se publicará en cuanto termine de generarse el texto "
                                       f"(como mucho {get_settings().caption_max_wait_minutes} min)."}
    return {"ok": True, "message": "Se enviará a Zernio en los próximos segundos."}


@router.post("/videos/{video_id}/retry")
async def retry_video(video_id: int, session: Session = Depends(get_session)) -> dict:
    item = _video_or_404(session, video_id)
    if item.status != VideoStatus.FAILED:
        raise HTTPException(409, "Sólo se pueden reintentar los vídeos fallidos.")
    if not Path(item.file_path).exists():
        raise HTTPException(400, "El archivo ya no existe en disco.")
    if item.delegated_at and item.zernio_post_id:
        # It failed while parked in Zernio (typically because the publication
        # was never confirmed). Queueing it again without cancelling there
        # could put the same video on TikTok twice.
        try:
            await accounts_svc.client(item.account, get_settings()).delete_post(
                item.zernio_post_id
            )
        except ZernioError as exc:
            raise HTTPException(
                409, f"Ese vídeo sigue en Zernio y no se ha podido cancelar ({exc.message}). "
                     "Bórralo en el panel de Zernio antes de reintentarlo, o se publicaría "
                     "dos veces.") from exc
        item.delegated_at = None

    item.status = VideoStatus.QUEUED
    item.attempts = 0
    item.last_error = None
    item.zernio_post_id = None
    item.scheduled_at = None
    item.preflight_errors = None
    schedule = _queue_of(item)
    item.schedule_id = schedule.id
    scheduling.plan_next_slot(session, schedule)
    session.commit()
    return video_json(item, schedule.timezone)


@router.post("/videos/{video_id}/caption/regenerate")
def regenerate_caption(video_id: int, session: Session = Depends(get_session)) -> dict:
    """Listen to the video again and write a new text and hashtags."""
    item = _video_or_404(session, video_id)
    settings = get_settings()
    if not settings.caption_ai_enabled:
        raise HTTPException(400, "Los textos automáticos están desactivados "
                                 "(CAPTION_AI_ENABLED=false en .env).")
    handed_over = item.status == VideoStatus.DELEGATED and item.zernio_post_id
    if item.status not in (VideoStatus.QUEUED, VideoStatus.SCHEDULED, VideoStatus.DELEGATED):
        raise HTTPException(409, f"El vídeo está en estado {item.status.value}.")
    if handed_over:
        raise HTTPException(409, "Este vídeo ya está en Zernio con su texto. Recupéralo "
                                 "primero si quieres escribirlo de nuevo.")
    if not Path(item.file_path).exists():
        raise HTTPException(400, "El archivo ya no existe en disco.")
    item.caption_status = "pending"
    item.caption_requested_at = datetime.now(timezone.utc)
    item.caption_error = None
    # Asked for on this one video: ahead of the library's bulk work.
    item.caption_priority = 1
    # From now on this queue writes its own: it stops following the library.
    if item.caption_source == library.FROM_LIBRARY:
        item.caption_source = None
    events.record(session, "caption_requested",
                  f"Texto automático pedido de nuevo para «{item.original_name}».",
                  account_id=item.account_id, video_id=item.id)
    session.commit()
    return video_json(item, _queue_of(item).timezone)


@router.get("/videos/{video_id}/file")
def video_file(video_id: int, session: Session = Depends(get_session)):
    """Serve the video itself so the UI can preview it before it goes out."""
    item = _video_or_404(session, video_id)
    path = _media_path_or_403(item)
    return FileResponse(str(path), media_type=publisher.mime_for(path), filename=path.name,
                        content_disposition_type="inline")


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #

@router.get("/analytics")
def get_analytics(
    account_id: int | None = None, top: int = 10, session: Session = Depends(get_session)
) -> dict:
    """Totals, best videos and the hour/weekday breakdown of what went out."""
    settings = get_settings()
    tz_name = settings.default_timezone
    if account_id is not None:
        account = _account_or_404(session, account_id)
        if account.default_schedule is not None:
            tz_name = account.default_schedule.timezone

    data = analytics_svc.summary(session, account_id=account_id, timezone_name=tz_name,
                                 top=max(1, min(top, 50)))
    data["top"] = [
        {**row, "published_at": iso(row["published_at"]),
         "published_local": local_str(row["published_at"], tz_name)}
        for row in data["top"]
    ]
    data["best_hours"] = analytics_svc.best_hours(data["by_hour"])
    data["updated_at"] = iso(data["updated_at"])
    data["window_days"] = settings.analytics_window_days
    return data


@router.post("/analytics/refresh")
async def refresh_analytics(
    account_id: int | None = None, session: Session = Depends(get_session)
) -> dict:
    """Pull the numbers from Zernio now instead of waiting for the worker."""
    settings = get_settings()
    if account_id is not None:
        account = _account_or_404(session, account_id)
        try:
            touched = await analytics_svc.sync_account(session, account, settings)
        except ZernioError as exc:
            raise _zernio_http_error(exc) from exc
        problems: list[str] = []
    else:
        touched, problems = await analytics_svc.sync_all(session, settings)
    events.record(session, "analytics_refreshed",
                  f"Analíticas actualizadas ({touched} vídeo(s)).", account_id=account_id)
    session.commit()
    return {"updated": touched, "problems": problems}


# --------------------------------------------------------------------------- #
# Comments
# --------------------------------------------------------------------------- #

def _comment_or_404(session: Session, comment_id: int) -> Comment:
    comment = session.get(Comment, comment_id)
    if comment is None:
        raise HTTPException(404, "Comentario no encontrado")
    return comment


@router.get("/comments")
def get_comments(
    account_id: int | None = None, video_id: int | None = None, pending: bool = False,
    mine: bool = True, limit: int = 200, session: Session = Depends(get_session),
) -> dict:
    """The mirrored comments, newest first; ``mine=false`` leaves out the
    account's own (``own`` still counts them, for the «Mostrar los míos» box)."""
    settings = get_settings()
    rows = comments_svc.recent(session, account_id=account_id, video_id=video_id,
                               pending_only=pending, include_own=mine,
                               limit=max(1, min(limit, 500)))
    return {
        "enabled": settings.comments_enabled,
        "pending": comments_svc.pending_count(session, account_id),
        "own": comments_svc.own_count(session, account_id),
        "comments": [comment_json(c, settings.default_timezone) for c in rows],
    }


@router.post("/comments/refresh")
async def refresh_comments(
    account_id: int | None = None, session: Session = Depends(get_session)
) -> dict:
    """Pull comments for the recent videos of one account, or of all of them."""
    settings = get_settings()
    targets = (
        [_account_or_404(session, account_id)] if account_id is not None
        else accounts_svc.list_all(session)
    )
    new, problems = 0, []
    for account in targets:
        if not account.is_active:
            continue
        try:
            new += await comments_svc.sync_account(session, account, settings,
                                                   limit=settings.comments_batch)
        except ZernioError as exc:
            problems.append(f"{account.display_name or account.username}: {exc.message}")
    session.commit()
    if problems and not new:
        raise HTTPException(502, " · ".join(problems))
    return {"new": new, "problems": problems}


@router.post("/comments/{comment_id}/reply")
async def reply_comment(
    comment_id: int, body: CommentReplyIn, session: Session = Depends(get_session)
) -> dict:
    comment = _comment_or_404(session, comment_id)
    account = _account_or_404(session, comment.account_id)
    try:
        await comments_svc.reply(session, account, comment, body.message, get_settings())
    except ZernioError as exc:
        raise _zernio_http_error(exc) from exc
    session.commit()
    return comment_json(comment, get_settings().default_timezone)


@router.post("/comments/{comment_id}/hide")
async def hide_comment(
    comment_id: int, body: CommentHideIn, session: Session = Depends(get_session)
) -> dict:
    comment = _comment_or_404(session, comment_id)
    account = _account_or_404(session, comment.account_id)
    try:
        await comments_svc.set_hidden(session, account, comment, body.hidden, get_settings())
    except ZernioError as exc:
        raise _zernio_http_error(exc) from exc
    session.commit()
    return comment_json(comment, get_settings().default_timezone)


@router.post("/comments/{comment_id}/answered")
def mark_comment_answered(
    comment_id: int, body: CommentAnsweredIn, session: Session = Depends(get_session)
) -> dict:
    """Mark a comment answered (or not) by hand, without writing anything.

    For the ones answered in the TikTok app, or that need no answer. Local
    only: Zernio is not told, and a later sync never undoes it.
    """
    comment = _comment_or_404(session, comment_id)
    comments_svc.set_answered(session, comment, body.answered)
    session.commit()
    return comment_json(comment, get_settings().default_timezone)
