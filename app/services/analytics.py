"""How the published videos actually did.

Zernio exposes TikTok's per-post numbers at ``GET /analytics?platform=tiktok``:
views, likes, comments and shares for every post, plus profile views and
completion rate on accounts connected through TikTok's Business app. This
module pulls them in (:func:`sync_account`) and turns them into the few answers
worth having (:func:`summary`):

* which videos worked,
* whether the hours and weekdays the scheduler is posting at are the good ones.

Everything TikTok Studio shows beyond that — impressions, reach, watch time,
where the views came from — is not on any public TikTok API, so it cannot be
here either. See docs/ANALYTICS.md.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Account, VideoItem, VideoMetric, VideoStatus
from . import accounts as accounts_svc
from . import hashtags as hashtags_svc
from .zernio import PostMetrics, ZernioError, tiktok_video_id

log = logging.getLogger("scheduler")

PAGE_SIZE = 100
MAX_PAGES = 10
# Zernio caps the window at 366 days.
MAX_WINDOW_DAYS = 366


def _published(session: Session, account_id: int) -> dict[str, VideoItem]:
    """Published videos of one account, by every id Zernio may name them with.

    Two, and they are not interchangeable: the analytics endpoint identifies a
    post by Zernio's own id (``latePostId``, which is what was stored when the
    post was created) and by TikTok's numeric video id (``postId``, the tail of
    the public URL). Indexing both means a video is found whichever one comes
    back, instead of the whole sync quietly matching nothing.
    """
    rows = session.scalars(
        select(VideoItem).where(
            VideoItem.account_id == account_id,
            VideoItem.status == VideoStatus.PUBLISHED,
            VideoItem.zernio_post_id.is_not(None),
        )
    )
    index: dict[str, VideoItem] = {}
    for item in rows:
        if item.zernio_post_id:
            index[item.zernio_post_id] = item
        video_id = tiktok_video_id(item.post_url)
        if video_id:
            index[video_id] = item
    return index


def _store(session: Session, item: VideoItem, metrics: PostMetrics) -> None:
    row = session.get(VideoMetric, item.id)
    if row is None:
        row = VideoMetric(video_id=item.id)
        session.add(row)
    row.views = metrics.views
    row.likes = metrics.likes
    row.comments = metrics.comments
    row.shares = metrics.shares
    row.profile_views = metrics.profile_views
    row.completion_rate = metrics.completion_rate
    # Zernio's own rate divides by whichever of impressions/reach/views is first
    # non-zero. On TikTok that is always views, but compute it when it is absent
    # so an older post is not shown as 0 % engagement.
    row.engagement_rate = metrics.engagement_rate or (
        round(metrics.interactions / metrics.views * 100, 2) if metrics.views else 0.0
    )
    row.platform_updated_at = metrics.updated_at
    row.fetched_at = datetime.now(timezone.utc)


async def sync_account(
    session: Session, account: Account, settings: Settings, days: int | None = None
) -> int:
    """Refresh this account's metrics from Zernio. Returns rows touched.

    Videos the tool did not publish are ignored: they have no queue, no local
    file and nothing to compare against.

    Every page is fetched before anything is written, and the writes are
    committed straight away: SQLite has one writer at a time, and a write
    left open across the next request would keep the rest of the app (and,
    through the event loop, the UI) waiting on Zernio.
    """
    by_post_id = _published(session, account.id)
    if not by_post_id:
        return 0

    window = min(days or settings.analytics_window_days, MAX_WINDOW_DAYS)
    from_date = (date.today() - timedelta(days=window)).isoformat()
    client = accounts_svc.client(account, settings)

    fetched: list[PostMetrics] = []
    for page in range(1, MAX_PAGES + 1):
        batch = await client.fetch_analytics(
            account_id=account.zernio_account_id, from_date=from_date,
            limit=PAGE_SIZE, page=page, platform=account.platform,
        )
        fetched.extend(batch)
        if len(batch) < PAGE_SIZE:
            break

    touched = 0
    for metrics in fetched:
        item = (by_post_id.get(metrics.post_id)
                or by_post_id.get(metrics.external_post_id))
        if item is None:
            continue
        if not metrics.measured:
            # TikTok has not reported on this one yet. Its zeros are not a
            # result, and writing them would erase whatever is stored.
            continue
        _store(session, item, metrics)
        touched += 1
    session.commit()
    return touched


async def sync_all(session: Session, settings: Settings) -> tuple[int, list[str]]:
    """Refresh every active account, surviving the ones that fail."""
    total, problems = 0, []
    # A list, not the live cursor: each account commits as it finishes.
    for account in session.scalars(select(Account).where(Account.is_active.is_(True))).all():
        try:
            total += await sync_account(session, account, settings)
        except ZernioError as exc:
            problems.append(f"{account.display_name or account.username}: {exc.message}")
        except Exception as exc:  # noqa: BLE001 - analytics must never break a tick
            log.warning("analytics sync failed for account %s: %s", account.id, exc)
            problems.append(f"{account.display_name or account.username}: {exc}")
    return total, problems


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #

def _rows(session: Session, account_id: int | None) -> list[tuple[VideoItem, VideoMetric]]:
    stmt = (
        select(VideoItem, VideoMetric)
        .join(VideoMetric, VideoMetric.video_id == VideoItem.id)
        .where(VideoItem.published_at.is_not(None))
    )
    if account_id is not None:
        stmt = stmt.where(VideoItem.account_id == account_id)
    return list(session.execute(stmt).all())


def _bucket(pairs, key) -> list[dict]:
    """Average views per bucket, so buckets with few posts stay comparable."""
    grouped: dict[int, list[int]] = defaultdict(list)
    for item, metric, local in pairs:
        grouped[key(local)].append(metric.views)
    out = []
    for value, views in sorted(grouped.items()):
        out.append({
            "bucket": value,
            "posts": len(views),
            "views_total": sum(views),
            "views_avg": round(sum(views) / len(views)),
        })
    return out


def by_hashtag(rows, limit: int = 30) -> list[dict]:
    """Average views of the videos each hashtag went out on.

    ``trusted`` marks tags with enough videos behind them to mean something;
    the same threshold the hashtag selector uses when it learns from this.
    """
    totals: dict[str, dict] = {}
    for item, metric in rows:
        for tag in hashtags_svc.extract(item.rendered_caption):
            entry = totals.setdefault(hashtags_svc.fold(tag),
                                      {"tag": tag, "posts": 0, "views_total": 0})
            entry["posts"] += 1
            entry["views_total"] += metric.views
    out = [
        {**e, "views_avg": round(e["views_total"] / e["posts"]),
         "trusted": e["posts"] >= hashtags_svc.MIN_USES_TO_TRUST}
        for e in totals.values()
    ]
    out.sort(key=lambda e: (-e["trusted"], -e["views_avg"], -e["posts"]))
    return out[:limit]


def hashtag_stats(session: Session, platform: str | None = None, limit: int = 60) -> list[dict]:
    """:func:`by_hashtag` over every account of one platform (or of all of them).

    The editor suggests from here: a text written in the library does not
    know yet which account will publish it, only the platform.
    """
    stmt = (
        select(VideoItem, VideoMetric)
        .join(VideoMetric, VideoMetric.video_id == VideoItem.id)
        .where(VideoItem.published_at.is_not(None))
    )
    if platform:
        stmt = stmt.join(Account, Account.id == VideoItem.account_id).where(
            Account.platform == platform)
    return by_hashtag(list(session.execute(stmt).all()), limit=limit)


def summary(session: Session, account_id: int | None = None,
            timezone_name: str = "Europe/Madrid", top: int = 10) -> dict:
    """Totals, the best videos, and how the hours and weekdays compare."""
    tz = ZoneInfo(timezone_name)
    rows = _rows(session, account_id)
    pairs = [(item, metric, item.published_at.astimezone(tz)) for item, metric in rows]

    views = sum(m.views for _, m in rows)
    interactions = sum(m.likes + m.comments + m.shares for _, m in rows)
    newest = max((m.fetched_at for _, m in rows), default=None)

    ranked = sorted(rows, key=lambda pair: pair[1].views, reverse=True)
    return {
        "timezone": timezone_name,
        "posts": len(rows),
        "totals": {
            "views": views,
            "likes": sum(m.likes for _, m in rows),
            "comments": sum(m.comments for _, m in rows),
            "shares": sum(m.shares for _, m in rows),
            "profile_views": sum(m.profile_views for _, m in rows),
            "views_avg": round(views / len(rows)) if rows else 0,
            "engagement_rate": round(interactions / views * 100, 2) if views else 0.0,
        },
        "by_hour": _bucket(pairs, lambda local: local.hour),
        "by_weekday": _bucket(pairs, lambda local: local.weekday()),
        "by_hashtag": by_hashtag(rows),
        "top": [
            {
                "video_id": item.id,
                "account_id": item.account_id,
                "name": item.original_name,
                "post_url": item.post_url,
                "published_at": item.published_at,
                "views": metric.views,
                "likes": metric.likes,
                "comments": metric.comments,
                "shares": metric.shares,
                "engagement_rate": metric.engagement_rate,
                "completion_rate": metric.completion_rate,
            }
            for item, metric in ranked[:top]
        ],
        "updated_at": newest,
    }


def best_hours(buckets: list[dict], min_posts: int = 3, take: int = 3) -> list[int]:
    """Hours worth trusting: enough posts behind them, best average first."""
    solid = [b for b in buckets if b["posts"] >= min_posts]
    solid.sort(key=lambda b: b["views_avg"], reverse=True)
    return [b["bucket"] for b in solid[:take]]
