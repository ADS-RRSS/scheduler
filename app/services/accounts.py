"""Accounts: TikTok accounts connected through Zernio, plus their API keys."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..crypto import decrypt, encrypt
from ..models import Account, Schedule
from . import events, scheduling
from .zernio import ZernioClient, ZernioError

PREFIX = "zernio:"


def get(session: Session, account_id: int) -> Account | None:
    return session.get(Account, account_id)


def list_all(session: Session) -> list[Account]:
    return list(session.scalars(select(Account).order_by(Account.id)))


def by_zernio_id(session: Session, zernio_account_id: str) -> Account | None:
    return session.scalars(
        select(Account).where(Account.external_id == f"{PREFIX}{zernio_account_id}")
    ).first()


def create(session: Session, *, api_key: str, zernio_account: dict, label: str = "") -> Account:
    """Connect one TikTok account found in Zernio. Raises ValueError if it exists.

    A TikTok account is connected once and only once. To publish on different
    schedules — mañana y noche, por ejemplo — add another queue to the account
    instead of connecting it twice (see ``services.scheduling.create_schedule``).
    """
    zernio_id = str(zernio_account["id"])
    if by_zernio_id(session, zernio_id) is not None:
        raise ValueError(
            "Esa cuenta ya está conectada. Si quieres publicarla con otro "
            "horario, añádele una segunda cola en Programación."
        )
    username = (zernio_account.get("username") or "").lstrip("@") or None
    account = Account(
        external_id=f"{PREFIX}{zernio_id}",
        zernio_account_id=zernio_id,
        api_key_enc=encrypt(api_key.strip()),
        username=username,
        display_name=(label or "").strip() or zernio_account.get("display_name")
        or (f"@{username}" if username else "Cuenta"),
        profile_name=zernio_account.get("profile") or None,
        platform=zernio_account.get("platform") or "tiktok",
        is_active=True,
        last_checked_at=datetime.now(timezone.utc),
    )
    session.add(account)
    session.flush()
    session.add(_default_schedule(account.id))
    session.flush()
    events.record(
        session, "account_connected",
        f"Cuenta conectada a través de Zernio: {account.display_name}",
        level="success", account_id=account.id,
    )
    return account


def _default_schedule(account_id: int) -> Schedule:
    """The queue every new account starts with; more can be added later."""
    return Schedule(
        account_id=account_id,
        name=scheduling.DEFAULT_NAME,
        position=0,
        enabled=False,
        interval_min_minutes=55,
        interval_max_minutes=75,
        jitter_seconds=90,
        max_per_day=6,
        windows=[],            # "indiferente" by default
        days_of_week=[0, 1, 2, 3, 4, 5, 6],
        timezone="Europe/Madrid",
    )


# --------------------------------------------------------------------------- #
# API key
# --------------------------------------------------------------------------- #

def api_key(account: Account) -> str:
    return decrypt(account.api_key_enc) or ""


def set_api_key(session: Session, account: Account, new_key: str) -> None:
    account.api_key_enc = encrypt(new_key.strip())
    account.last_checked_at = datetime.now(timezone.utc)
    session.flush()
    events.record(session, "account_key_replaced",
                  f"API key de Zernio sustituida en {account.display_name}.",
                  level="warn", account_id=account.id)


def client(account: Account, settings: Settings) -> ZernioClient:
    key = api_key(account)
    if not key:
        raise ZernioError("zernio_auth", "La cuenta no tiene API key de Zernio; añádela en Cuentas.")
    return ZernioClient(key, settings.zernio_api_base, timeout=settings.zernio_timeout_seconds)


async def verify(session: Session, account: Account, settings: Settings) -> dict:
    """Check the stored key still lists this account in Zernio."""
    found = await client(account, settings).list_accounts(account.platform)
    match = next((a for a in found if a["id"] == account.zernio_account_id), None)
    account.last_checked_at = datetime.now(timezone.utc)
    if match:
        account.username = match.get("username") or account.username
        account.profile_name = match.get("profile") or account.profile_name
    session.flush()
    return {
        "ok": match is not None,
        "account": match,
        "accounts_in_zernio": len(found),
        "message": ("Conexión correcta." if match else
                    "La API key funciona, pero esta cuenta ya no aparece en Zernio. "
                    "Reconéctala en el panel de Zernio."),
    }


def disconnect(session: Session, account: Account) -> None:
    events.record(session, "account_disconnect",
                  f"Cuenta eliminada: {account.display_name}", account_id=account.id, level="warn")
    session.delete(account)
