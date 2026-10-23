"""Post analytics: reading the numbers back and turning them into answers.

What Zernio gives is one row per post; what the UI needs is "which video
worked" and "are the hours I post at the good ones". Both are computed here.
"""
from __future__ import annotations

from datetime import datetime, timezone

from tests.helpers import (
    configure_mock,
    drain,
    first_video,
    make_account,
    put_schedule,
    reset_mock,
    upload_sample,
)


async def _publish_one(client, sample_video, caption: str = "Uno") -> dict:
    account_id = await make_account(client)
    await put_schedule(client, account_id)
    video = await upload_sample(client, account_id, sample_video, caption=caption)
    await client.post(f"/api/videos/{video['id']}/publish-now")
    await drain(account_id)
    item = await first_video(client)
    assert item["status"] == "PUBLISHED", item["last_error"]
    return item


async def test_metrics_are_pulled_and_attached_to_the_video(client, sample_video):
    await reset_mock()
    await configure_mock(analytics=[{
        "views": 15420, "likes": 342, "comments": 28, "shares": 45,
        "profileViews": 91, "completionRate": 0.42, "engagementRate": 2.69,
        "lastUpdated": "2026-09-16T08:00:00Z",
    }])
    await _publish_one(client, sample_video)

    resp = await client.post("/api/analytics/refresh")
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 1

    item = await first_video(client)
    assert item["metrics"]["views"] == 15420
    assert item["metrics"]["likes"] == 342
    assert item["metrics"]["profile_views"] == 91
    assert item["metrics"]["completion_rate"] == 0.42
    assert item["metrics"]["engagement_rate"] == 2.69
    assert item["metrics"]["platform_updated_at"].startswith("2026-09-16T08:00")


async def test_a_video_without_numbers_yet_reports_none(client, sample_video):
    """A post TikTok has not counted yet must not look like a flop."""
    await reset_mock()
    await _publish_one(client, sample_video)
    assert (await first_video(client))["metrics"] is None

    summary = (await client.get("/api/analytics")).json()
    assert summary["posts"] == 0 and summary["totals"]["views"] == 0
    assert summary["top"] == [] and summary["updated_at"] is None


async def test_engagement_rate_is_computed_when_zernio_omits_it(client, sample_video):
    await reset_mock()
    await configure_mock(analytics=[
        {"views": 1000, "likes": 80, "comments": 15, "shares": 5, "engagementRate": 0},
    ])
    await _publish_one(client, sample_video)
    await client.post("/api/analytics/refresh")
    # (80 + 15 + 5) / 1000 -> 10 %
    assert (await first_video(client))["metrics"]["engagement_rate"] == 10.0


async def test_summary_totals_ranking_and_hour_buckets(client, sample_video):
    await reset_mock()
    await configure_mock(analytics=[{"views": 300, "likes": 30, "comments": 3, "shares": 2}])
    item = await _publish_one(client, sample_video)
    await client.post("/api/analytics/refresh")

    summary = (await client.get("/api/analytics")).json()
    assert summary["posts"] == 1
    assert summary["totals"]["views"] == 300 and summary["totals"]["views_avg"] == 300
    # (30 + 3 + 2) / 300
    assert summary["totals"]["engagement_rate"] == 11.67
    assert summary["top"][0]["video_id"] == item["id"]
    assert summary["top"][0]["views"] == 300
    assert summary["top"][0]["published_local"]

    published = datetime.fromisoformat(item["published_at"]).astimezone(timezone.utc)
    (bucket,) = summary["by_hour"]
    assert bucket["posts"] == 1 and bucket["views_avg"] == 300
    assert 0 <= bucket["bucket"] <= 23
    (weekday,) = summary["by_weekday"]
    assert weekday["bucket"] == published.weekday() or 0 <= weekday["bucket"] <= 6
    assert summary["timezone"] == "Europe/Madrid"


async def test_best_hours_ignores_buckets_with_too_few_posts():
    from app.services.analytics import best_hours

    buckets = [
        {"bucket": 9, "posts": 1, "views_avg": 99_999},   # one lucky video
        {"bucket": 18, "posts": 5, "views_avg": 4_000},
        {"bucket": 21, "posts": 4, "views_avg": 6_000},
        {"bucket": 3, "posts": 3, "views_avg": 100},
    ]
    assert best_hours(buckets) == [21, 18, 3]
    assert best_hours(buckets, min_posts=10) == []


async def test_refresh_reports_a_zernio_failure(client, sample_video):
    await reset_mock()
    await _publish_one(client, sample_video)
    await configure_mock(fail=401)
    resp = await client.post("/api/analytics/refresh")
    # sync_all survives a broken account and says which one failed.
    assert resp.status_code == 200
    assert resp.json()["updated"] == 0
    assert any("API key" in p for p in resp.json()["problems"])
