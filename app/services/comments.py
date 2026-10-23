"""TikTok comments on the videos this tool published.

Zernio's inbox exposes them at ``GET /inbox/comments/{postId}`` and takes a
reply at ``POST /inbox/comments/{postId}``. Two things shape this module:

* **There is no webhook here.** Zernio can push ``comment.received``, but that
  needs a public HTTPS URL and this tool runs on 127.0.0.1, so it polls. The
  endpoint is cached for ten minutes on Zernio's side, which sets the floor for
  how often polling is worth anything (``comments_poll_minutes``).
* **Only recent videos are worth polling.** Comments arrive in the first days;
  after ``comments_window_days`` a video stops being checked, so the number of
  requests stays flat no matter how long the queue has been running.

Comments are mirrored into the local database so the UI works offline and a
reply is only ever sent once.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Account, Comment, VideoItem, VideoStatus
from . import accounts as accounts_svc
from . import events
from .zernio import CommentItem, ZernioError, tiktok_video_id

log = logging.getLogger("scheduler")

FETCH_LIMIT = 50


def post_ref(item: VideoItem) -> str | None:
    """What to put in the URL: TikTok's video id, or Zernio's own post id.

    Zernio resolves its own ids too, so the TikTok one is only preferred
    because it is what the endpoint documents for TikTok.
    """
    return tiktok_video_id(item.post_url) or item.zernio_post_id


def due_videos(session: Session, account_id: int, settings: Settings,
               limit: int = 5) -> list[VideoItem]:
    """Published videos whose comments should be refreshed now."""
    now = datetime.now(timezone.utc)
    horizon = now - timedelta(days=settings.comments_window_days)
    stale = now - timedelta(minutes=settings.comments_poll_minutes)
    rows = session.scalars(
        select(VideoItem)
        .where(
            VideoItem.account_id == account_id,
            VideoItem.status == VideoStatus.PUBLISHED,
            VideoItem.published_at.is_not(None),
            VideoItem.published_at >= horizon,
        )
        .order_by(VideoItem.published_at.desc())
    )
    due = [
        item for item in rows
        if post_ref(item)
        and (item.comments_checked_at is None or item.comments_checked_at <= stale)
    ]
    return due[:limit]


def _upsert(session: Session, item: VideoItem, incoming: CommentItem) -> bool:
    """Store one comment. Returns True when it was not already known."""
    existing = session.scalars(
        select(Comment).where(
            Comment.video_id == item.id, Comment.external_id == incoming.external_id
        )
    ).first()
    if existing is None:
        session.add(Comment(
            video_id=item.id,
            account_id=item.account_id,
            external_id=incoming.external_id,
            parent_external_id=incoming.parent_external_id,
            author_name=incoming.author_name,
            author_username=incoming.author_username,
            message=incoming.message,
            like_count=incoming.like_count,
            reply_count=incoming.reply_count,
            is_owner=incoming.is_owner,
            created_at=incoming.created_at,
        ))
        return True
    # Counters move; the text does not.
    existing.like_count = incoming.like_count
    existing.reply_count = incoming.reply_count
    existing.fetched_at = datetime.now(timezone.utc)
    return False


def _mark_answered(session: Session, item: VideoItem) -> None:
    """A comment we replied to from anywhere counts as answered."""
    parents = {
        c.parent_external_id
        for c in session.scalars(select(Comment).where(Comment.video_id == item.id))
        if c.is_owner and c.parent_external_id
    }
    if not parents:
        return
    for comment in session.scalars(select(Comment).where(Comment.video_id == item.id)):
        if comment.external_id in parents and not comment.answered:
            comment.answered = True


async def sync_video(session: Session, account: Account, item: VideoItem,
                     settings: Settings) -> int:
    """Pull one video's comments. Returns how many were new."""
    ref = post_ref(item)
    if not ref:
        return 0
    client = accounts_svc.client(account, settings)
    incoming = await client.fetch_comments(ref, account.zernio_account_id, limit=FETCH_LIMIT)
    new = sum(_upsert(session, item, comment) for comment in incoming)
    _mark_answered(session, item)
    item.comments_checked_at = datetime.now(timezone.utc)
    # Committed here, not at the end of the account: the next video's request
    # would otherwise run with SQLite's only write lock still taken.
    session.commit()
    return new


async def sync_account(session: Session, account: Account, settings: Settings,
                       limit: int = 5) -> int:
    """Refresh the comments of this account's most recent videos."""
    new = 0
    for item in due_videos(session, account.id, settings, limit=limit):
        try:
            new += await sync_video(session, account, item, settings)
        except ZernioError as exc:
            # A lane or plan problem is the same for every video: stop asking.
            if exc.code in ("zernio_inbox_unavailable", "zernio_inbox_forbidden",
                            "zernio_auth"):
                item.comments_checked_at = datetime.now(timezone.utc)
                session.commit()
                raise
            log.warning("comment sync failed for video %s: %s", item.id, exc.message)
            item.comments_checked_at = datetime.now(timezone.utc)
            session.commit()
    return new


async def reply(session: Session, account: Account, comment: Comment, message: str,
                settings: Settings) -> Comment | None:
    """Answer one comment, and remember the answer."""
    item = comment.video
    ref = post_ref(item)
    if not ref:
        raise ZernioError("comment_no_post", "Ese vídeo todavía no tiene URL en TikTok.")
    client = accounts_svc.client(account, settings)
    new_id = await client.reply_comment(
        ref, account.zernio_account_id, message, comment_id=comment.external_id
    )
    comment.answered = True
    mine = None
    if new_id:
        mine = Comment(
            video_id=item.id, account_id=account.id, external_id=new_id,
            parent_external_id=comment.external_id,
            author_name=account.display_name or (account.username or ""),
            author_username=account.username or "",
            message=message, is_owner=True, created_at=datetime.now(timezone.utc),
        )
        session.add(mine)
    events.record(
        session, "comment_replied",
        f"Respuesta enviada a @{comment.author_username or comment.author_name} "
        f"en «{item.original_name}».",
        level="success", account_id=account.id, video_id=item.id,
    )
    session.flush()
    return mine


async def set_hidden(session: Session, account: Account, comment: Comment, hidden: bool,
                     settings: Settings) -> None:
    item = comment.video
    ref = post_ref(item)
    if not ref:
        raise ZernioError("comment_no_post", "Ese vídeo todavía no tiene URL en TikTok.")
    client = accounts_svc.client(account, settings)
    await client.set_comment_hidden(ref, comment.external_id, account.zernio_account_id,
                                    hidden)
    comment.hidden = hidden
    events.record(
        session, "comment_hidden",
        f"Comentario de @{comment.author_username or comment.author_name} "
        + ("ocultado" if hidden else "vuelto a mostrar") + f" en «{item.original_name}».",
        level="warn" if hidden else "info", account_id=account.id, video_id=item.id,
    )
    session.flush()


def set_answered(session: Session, comment: Comment, answered: bool) -> None:
    """Mark by hand: answered in the TikTok app, or nothing to answer."""
    if comment.answered == answered:
        return
    comment.answered = answered
    events.record(
        session, "comment_marked",
        f"Comentario de @{comment.author_username or comment.author_name} en "
        f"«{comment.video.original_name}» marcado como "
        + ("respondido." if answered else "sin responder."),
        account_id=comment.account_id, video_id=comment.video_id,
    )
    session.flush()

def recent(session: Session, account_id: int | None = None, video_id: int | None = None,
           pending_only: bool = False, include_own: bool = True,
           limit: int = 200) -> list[Comment]:
    """Comments for the UI, newest first.

    ``include_own=False`` leaves out the ones the connected account wrote (the
    pinned questions and its replies): they never need an answer.
    """
    stmt = select(Comment)
    if account_id is not None:
        stmt = stmt.where(Comment.account_id == account_id)
    if video_id is not None:
        stmt = stmt.where(Comment.video_id == video_id)
    if not include_own:
        stmt = stmt.where(Comment.is_owner.is_(False))
    if pending_only:
        stmt = stmt.where(
            Comment.is_owner.is_(False), Comment.answered.is_(False),
            Comment.hidden.is_(False),
        )
    stmt = stmt.order_by(Comment.created_at.desc().nullslast(), Comment.id.desc())
    return list(session.scalars(stmt.limit(limit)))


def pending_count(session: Session, account_id: int | None = None) -> int:
    return len(recent(session, account_id=account_id, pending_only=True, limit=1000))


def own_count(session: Session, account_id: int | None = None) -> int:
    """How many the connected accounts wrote themselves."""
    stmt = select(func.count()).select_from(Comment).where(Comment.is_owner.is_(True))
    if account_id is not None:
        stmt = stmt.where(Comment.account_id == account_id)
    return session.scalar(stmt) or 0
