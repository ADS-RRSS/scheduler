"""The weekly calendar: every account's posts, past, parked in Zernio and still to come."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..scheduler import worker as worker_mod
from ..services import accounts as accounts_svc
from ..services import calendar
from .serializers import account_json

router = APIRouter(prefix="/api", tags=["calendar"])


@router.get("/calendar")
def calendar_week(
    start: str | None = None,
    days: int = 7,
    platform: str | None = None,
    account_id: list[int] | None = Query(default=None),
    projected: bool = True,
    session: Session = Depends(get_session),
) -> dict:
    """``days`` days from ``start`` (the Monday of this week by default).

    ``platform`` (tiktok, youtube) and ``account_id`` (repeatable) narrow the
    accounts; ``projected=false`` leaves out what is only simulated.
    """
    settings = get_settings()
    tz = ZoneInfo(settings.default_timezone)
    try:
        first = date.fromisoformat(start) if start else calendar.week_start(datetime.now(tz).date())
    except ValueError as exc:
        raise HTTPException(400, "start debe ser una fecha AAAA-MM-DD.") from exc
    if platform and platform not in ("tiktok", "youtube"):
        raise HTTPException(400, "platform debe ser tiktok o youtube.")
    everyone = accounts_svc.list_all(session)
    chosen = [a for a in everyone
              if (not platform or (a.platform or "tiktok") == platform)
              and (not account_id or a.id in set(account_id))]
    data = calendar.week(
        session, chosen, start=first, days=days, tz_name=settings.default_timezone,
        project=projected, paused=worker_mod.is_paused(session),
    )
    labels = {a.id: account_json(a)["label"] for a in chosen}
    for account in data["accounts"]:
        account["label"] = labels.get(account["id"], f"Cuenta {account['id']}")
    return data
