"""The daily budget counter.

Counting is done in SQL against precomputed UTC bounds, so these tests pin down
the one thing that could silently go wrong: which local calendar day a UTC
timestamp belongs to.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.models import Account, Schedule, VideoItem, VideoStatus
from app.services import queue

MADRID = ZoneInfo("Europe/Madrid")


@pytest.fixture
def account(fresh_db):
    """Create the account, then close the session BEFORE yielding.

    Yielding from inside `session_scope()` would hold an open write transaction
    for the whole test, and the session the test opens would block on it.
    """
    with session_scope() as session:
        acc = Account(external_id="zernio:counting-test", zernio_account_id="counting-test",
                      username="test")
        session.add(acc)
        session.flush()
        session.add(Schedule(account_id=acc.id, name="Cola principal", position=0))
        session.flush()
        account_id = acc.id
    return account_id


@pytest.fixture
def two_queues(account):
    """The same account with a second queue. Returns (account_id, día, noche)."""
    with session_scope() as session:
        acc = session.get(Account, account)
        day = acc.schedules[0]
        night = Schedule(account_id=acc.id, name="Noche", position=1)
        session.add(night)
        session.flush()
        return account, day.id, night.id


def add(session, account_id, *, status, published=None, scheduled=None, name="v.mp4",
        schedule_id=None):
    if schedule_id is None:
        schedule_id = session.scalars(
            select(Schedule.id).where(Schedule.account_id == account_id)
            .order_by(Schedule.position, Schedule.id)
        ).first()
    item = VideoItem(
        account_id=account_id, schedule_id=schedule_id, file_path=f"/tmp/{name}",
        original_name=name, checksum=name + str(published or scheduled), status=status,
        published_at=published, scheduled_at=scheduled,
    )
    session.add(item)
    session.flush()
    return item


def madrid(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=MADRID).astimezone(timezone.utc)


def test_counts_published_items_on_their_local_day(account):
    with session_scope() as session:
        add(session, account, status=VideoStatus.PUBLISHED,
            published=madrid(2026, 6, 10, 9, 0), name="a.mp4")
        add(session, account, status=VideoStatus.PUBLISHED,
            published=madrid(2026, 6, 10, 21, 0), name="b.mp4")
        assert queue.committed_on(session, date(2026, 6, 10), MADRID, account_id=account) == 2
        assert queue.committed_on(session, date(2026, 6, 11), MADRID, account_id=account) == 0


def test_midnight_boundary_is_local_not_utc(account):
    """In summer Madrid is UTC+2, so late-evening local times are the previous
    UTC day. Counting by UTC day would put these in the wrong bucket."""
    with session_scope() as session:
        add(session, account, status=VideoStatus.PUBLISHED,
            published=madrid(2026, 6, 10, 23, 59), name="late.mp4")
        add(session, account, status=VideoStatus.PUBLISHED,
            published=madrid(2026, 6, 11, 0, 1), name="early.mp4")
        assert queue.committed_on(session, date(2026, 6, 10), MADRID, account_id=account) == 1
        assert queue.committed_on(session, date(2026, 6, 11), MADRID, account_id=account) == 1


def test_scheduled_items_count_towards_the_budget(account):
    with session_scope() as session:
        add(session, account, status=VideoStatus.SCHEDULED,
            scheduled=madrid(2026, 6, 10, 15, 0), name="s.mp4")
        assert queue.committed_on(session, date(2026, 6, 10), MADRID, account_id=account) == 1


def test_queued_and_failed_do_not_count(account):
    with session_scope() as session:
        add(session, account, status=VideoStatus.QUEUED,
            scheduled=madrid(2026, 6, 10, 10, 0), name="q.mp4")
        add(session, account, status=VideoStatus.FAILED,
            scheduled=madrid(2026, 6, 10, 11, 0), name="f.mp4")
        assert queue.committed_on(session, date(2026, 6, 10), MADRID, account_id=account) == 0


def test_in_flight_items_count(account):
    with session_scope() as session:
        add(session, account, status=VideoStatus.UPLOADING,
            scheduled=madrid(2026, 6, 10, 10, 0), name="u.mp4")
        add(session, account, status=VideoStatus.PROCESSING,
            scheduled=madrid(2026, 6, 10, 11, 0), name="p.mp4")
        assert queue.committed_on(session, date(2026, 6, 10), MADRID, account_id=account) == 2


def test_published_at_wins_over_scheduled_at(account):
    """An item scheduled for the 10th but actually published on the 11th counts
    on the 11th: the budget tracks what happened, not what was planned."""
    with session_scope() as session:
        add(session, account, status=VideoStatus.PUBLISHED,
            scheduled=madrid(2026, 6, 10, 23, 0),
            published=madrid(2026, 6, 11, 1, 0), name="drift.mp4")
        assert queue.committed_on(session, date(2026, 6, 10), MADRID, account_id=account) == 0
        assert queue.committed_on(session, date(2026, 6, 11), MADRID, account_id=account) == 1


def test_items_without_any_timestamp_are_ignored(account):
    with session_scope() as session:
        add(session, account, status=VideoStatus.SCHEDULED, name="none.mp4")
        assert queue.committed_on(session, date(2026, 6, 10), MADRID, account_id=account) == 0


def test_exclude_video_id_removes_only_that_item(account):
    with session_scope() as session:
        keep = add(session, account, status=VideoStatus.SCHEDULED,
                   scheduled=madrid(2026, 6, 10, 9, 0), name="keep.mp4")
        drop = add(session, account, status=VideoStatus.SCHEDULED,
                   scheduled=madrid(2026, 6, 10, 10, 0), name="drop.mp4")
        assert queue.committed_on(session, date(2026, 6, 10), MADRID, account_id=account) == 2
        assert queue.committed_on(session, date(2026, 6, 10), MADRID, account_id=account, exclude_video_id=drop.id) == 1
        assert queue.committed_on(session, date(2026, 6, 10), MADRID, account_id=account, exclude_video_id=keep.id) == 1


def test_other_accounts_do_not_leak(account):
    with session_scope() as session:
        other = Account(external_id="zernio:other-account", zernio_account_id="other-account",
                        username="other")
        session.add(other)
        session.flush()
        add(session, other.id, status=VideoStatus.PUBLISHED,
            published=madrid(2026, 6, 10, 9, 0), name="other.mp4")
        assert queue.committed_on(session, date(2026, 6, 10), MADRID, account_id=account) == 0
        assert queue.committed_on(session, date(2026, 6, 10), MADRID, account_id=other.id) == 1


def test_spring_forward_day_is_23_hours_long(account):
    """2026-03-29 in Madrid runs 00:00 -> 24:00 local but only 23 real hours."""
    start, end = queue.local_day_bounds(date(2026, 3, 29), MADRID)
    assert end - start == timedelta(hours=23)
    with session_scope() as session:
        add(session, account, status=VideoStatus.PUBLISHED,
            published=madrid(2026, 3, 29, 23, 30), name="dst1.mp4")
        assert queue.committed_on(session, date(2026, 3, 29), MADRID, account_id=account) == 1
        assert queue.committed_on(session, date(2026, 3, 30), MADRID, account_id=account) == 0


def test_autumn_fall_back_day_is_25_hours_long(account):
    start, end = queue.local_day_bounds(date(2026, 10, 25), MADRID)
    assert end - start == timedelta(hours=25)
    with session_scope() as session:
        # 02:30 happens twice that day; both belong to the 25th.
        add(session, account, status=VideoStatus.PUBLISHED,
            published=start + timedelta(hours=2, minutes=30), name="dst2.mp4")
        add(session, account, status=VideoStatus.PUBLISHED,
            published=start + timedelta(hours=3, minutes=30), name="dst3.mp4")
        assert queue.committed_on(session, date(2026, 10, 25), MADRID, account_id=account) == 2


def test_day_counter_is_memoised(account):
    """The planner can probe hundreds of days; each date must hit SQL once."""
    with session_scope() as session:
        add(session, account, status=VideoStatus.PUBLISHED,
            published=madrid(2026, 6, 10, 9, 0), name="m.mp4")
        calls = {"n": 0}
        original = queue.committed_on

        def counting(*args, **kwargs):
            calls["n"] += 1
            return original(*args, **kwargs)

        queue.committed_on = counting
        try:
            counter = queue.day_counter(session, MADRID, account_id=account)
            for _ in range(50):
                counter(date(2026, 6, 10))
                counter(date(2026, 6, 11))
        finally:
            queue.committed_on = original

        assert calls["n"] == 2, "una consulta por fecha distinta, no por llamada"
        assert counter(date(2026, 6, 10)) == 1


def test_counter_respects_a_different_timezone(account):
    """09:00 in Madrid is 02:00 in Mexico City: a different calendar day only
    when the offset pushes it across midnight."""
    mexico = ZoneInfo("America/Mexico_City")
    with session_scope() as session:
        add(session, account, status=VideoStatus.PUBLISHED,
            published=madrid(2026, 6, 10, 3, 0), name="tz.mp4")
        assert queue.committed_on(session, date(2026, 6, 10), MADRID, account_id=account) == 1
        assert queue.committed_on(session, date(2026, 6, 9), mexico, account_id=account) == 1
        assert queue.committed_on(session, date(2026, 6, 10), mexico, account_id=account) == 0


# --------------------------------------------------------------------------- #
# Several queues on one account
# --------------------------------------------------------------------------- #

def test_each_queue_counts_only_its_own_videos(two_queues):
    account_id, day, night = two_queues
    with session_scope() as session:
        add(session, account_id, status=VideoStatus.PUBLISHED, schedule_id=day,
            published=madrid(2026, 6, 10, 11, 0), name="d1.mp4")
        add(session, account_id, status=VideoStatus.PUBLISHED, schedule_id=day,
            published=madrid(2026, 6, 10, 15, 0), name="d2.mp4")
        add(session, account_id, status=VideoStatus.SCHEDULED, schedule_id=night,
            scheduled=madrid(2026, 6, 10, 23, 0), name="n1.mp4")

        assert queue.committed_on(session, date(2026, 6, 10), MADRID, schedule_id=day) == 2
        assert queue.committed_on(session, date(2026, 6, 10), MADRID, schedule_id=night) == 1
        # The account-wide budget sees every queue at once.
        assert queue.committed_on(session, date(2026, 6, 10), MADRID, account_id=account_id) == 3


def test_pending_is_scoped_to_one_queue(two_queues):
    account_id, day, night = two_queues
    with session_scope() as session:
        add(session, account_id, status=VideoStatus.QUEUED, schedule_id=day, name="d.mp4")
        add(session, account_id, status=VideoStatus.QUEUED, schedule_id=night, name="n1.mp4")
        add(session, account_id, status=VideoStatus.QUEUED, schedule_id=night, name="n2.mp4")

        assert [i.original_name for i in queue.pending(session, day)] == ["d.mp4"]
        assert [i.original_name for i in queue.pending(session, night)] == ["n1.mp4", "n2.mp4"]
        assert queue.next_up(session, night).original_name == "n1.mp4"
        # In-flight is per account: one upload at a time, whatever the queue.
        assert queue.in_flight(session, account_id) == []
