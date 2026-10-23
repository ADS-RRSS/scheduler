"""Exhaustive tests for the scheduling maths.

This is the part of the tool a user cannot verify by eye, so it gets the most
scrutiny: windows, midnight wrap, daily caps, weekday filters and DST.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.scheduler.planner import (
    MINUTES_PER_DAY,
    PolicyError,
    SchedulePolicy,
    Spacing,
    Window,
    build_policy,
    compute_next_slot,
    describe_policy,
    preview_slots,
)

MADRID = ZoneInfo("Europe/Madrid")


def utc(y, m, d, hh=0, mm=0, ss=0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)


def madrid(y, m, d, hh=0, mm=0, ss=0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=MADRID).astimezone(timezone.utc)


def local(dt: datetime) -> datetime:
    return dt.astimezone(MADRID)


def fixed_rng(value: float = 0.0) -> random.Random:
    """Deterministic RNG: uniform() -> low bound, randint() -> low bound."""

    class _R(random.Random):
        def uniform(self, a, b):  # noqa: D102
            return a + (b - a) * value

        def randint(self, a, b):  # noqa: D102
            return int(a + (b - a) * value)

    return _R()


# --------------------------------------------------------------------------- #
# Window arithmetic
# --------------------------------------------------------------------------- #

def test_window_normal_span():
    w = Window(360, 1260)  # 06:00 -> 21:00
    assert w.span_end == 1260
    assert not w.wraps
    assert w.duration_minutes == 900


def test_window_wraps_midnight():
    w = Window(1320, 360)  # 22:00 -> 06:00
    assert w.wraps
    assert w.span_end == 360 + MINUTES_PER_DAY
    assert w.duration_minutes == 480


def test_window_full_day_when_start_equals_end():
    w = Window(600, 600)
    assert w.duration_minutes == MINUTES_PER_DAY


def test_window_rejects_out_of_range():
    with pytest.raises(PolicyError):
        Window(-1, 600)
    with pytest.raises(PolicyError):
        Window(0, MINUTES_PER_DAY + 1)


def test_build_policy_collapses_full_day_window_to_any_time():
    p = build_policy(
        interval_min_minutes=30, interval_max_minutes=40,
        windows=[{"start": 0, "end": MINUTES_PER_DAY}],
    )
    assert p.windows == ()


def test_policy_rejects_inverted_interval():
    with pytest.raises(PolicyError):
        SchedulePolicy(interval_min_minutes=90, interval_max_minutes=30)


def test_policy_rejects_unknown_timezone():
    with pytest.raises(PolicyError):
        SchedulePolicy(timezone="Mars/Olympus")


def test_policy_rejects_empty_weekdays():
    with pytest.raises(PolicyError):
        SchedulePolicy(days_of_week=frozenset())


# --------------------------------------------------------------------------- #
# Interval behaviour
# --------------------------------------------------------------------------- #

def test_first_ever_post_goes_out_immediately():
    p = build_policy(interval_min_minutes=25, interval_max_minutes=35, jitter_seconds=0)
    now = madrid(2026, 6, 10, 12, 0)
    assert compute_next_slot(now=now, last_post_at=None, policy=p) == now


def test_interval_is_drawn_inside_the_configured_range():
    p = build_policy(interval_min_minutes=25, interval_max_minutes=35, jitter_seconds=0)
    now = madrid(2026, 6, 10, 12, 0)
    last = now - timedelta(hours=2)
    for seed in range(300):
        slot = compute_next_slot(
            now=now, last_post_at=last, policy=p, rng=random.Random(seed)
        )
        gap = (slot - last).total_seconds() / 60
        # Slot is clamped to `now` when the drawn gap already elapsed.
        assert 25 <= gap <= 35 or slot == now


def test_slot_never_lands_in_the_past():
    p = build_policy(interval_min_minutes=25, interval_max_minutes=35)
    now = madrid(2026, 6, 10, 12, 0)
    last = now - timedelta(days=3)
    slot = compute_next_slot(now=now, last_post_at=last, policy=p)
    assert slot >= now


def test_min_gap_overrides_a_shorter_random_draw():
    p = build_policy(
        interval_min_minutes=5, interval_max_minutes=6,
        min_gap_minutes=45, jitter_seconds=0,
    )
    last = madrid(2026, 6, 10, 12, 0)
    now = last  # nothing elapsed yet
    slot = compute_next_slot(now=now, last_post_at=last, policy=p, rng=fixed_rng(0))
    assert (slot - last) == timedelta(minutes=45)


def test_jitter_is_forward_only_and_bounded():
    p = build_policy(
        interval_min_minutes=60, interval_max_minutes=60, jitter_seconds=120
    )
    last = madrid(2026, 6, 10, 12, 0)
    now = last
    for seed in range(200):
        slot = compute_next_slot(
            now=now, last_post_at=last, policy=p, rng=random.Random(seed)
        )
        offset = (slot - (last + timedelta(minutes=60))).total_seconds()
        assert 0 <= offset <= 120


# --------------------------------------------------------------------------- #
# Hour windows
# --------------------------------------------------------------------------- #

def test_no_windows_means_any_hour_is_valid():
    p = build_policy(interval_min_minutes=30, interval_max_minutes=30, jitter_seconds=0)
    now = madrid(2026, 6, 10, 3, 14)
    slot = compute_next_slot(now=now, last_post_at=None, policy=p)
    assert local(slot).hour == 3


def test_slot_before_window_is_pushed_to_window_start():
    p = build_policy(
        interval_min_minutes=30, interval_max_minutes=30, jitter_seconds=0,
        windows=[{"start": 360, "end": 1260}],  # 06:00-21:00
    )
    now = madrid(2026, 6, 10, 4, 0)
    slot = compute_next_slot(now=now, last_post_at=None, policy=p)
    # Into the window, but not on the dot: up to OPENING_SPREAD_MINUTES later.
    assert madrid(2026, 6, 10, 6, 0) <= slot <= madrid(2026, 6, 10, 6, 30)


def test_without_spread_the_window_start_is_exact():
    p = build_policy(
        interval_min_minutes=30, interval_max_minutes=30, jitter_seconds=0,
        windows=[{"start": 360, "end": 1260}], opening_spread_minutes=0,
    )
    slot = compute_next_slot(now=madrid(2026, 6, 10, 4, 0), last_post_at=None, policy=p)
    assert slot == madrid(2026, 6, 10, 6, 0)


def test_waiting_for_a_window_does_not_always_land_on_its_opening():
    """Two «top» windows with a cadence that fits between them.

    The case that prompted this: 07-10 and 18-22, a video every couple of
    hours, so every day's first post of each window waited for it to open and
    went out at 07:00 and 18:00 on the dot.
    """
    p = build_policy(
        interval_min_minutes=100, interval_max_minutes=140, jitter_seconds=90,
        windows=[{"start": 420, "end": 600}, {"start": 1080, "end": 1320}],
    )
    rng = random.Random(7)
    slots = preview_slots(now=madrid(2026, 6, 10, 23, 0), last_post_at=None, policy=p,
                          count=60, rng=rng)
    openings = [local(s) for s in slots
                if (local(s).hour, local(s).minute) < (7, 31) and local(s).hour >= 7
                or (local(s).hour, local(s).minute) < (18, 31) and local(s).hour >= 18]
    assert len(openings) > 20
    minutes = [o.minute for o in openings]
    # Spread over the first half hour, never past it, and not stuck on :00.
    assert max(minutes) <= 30
    assert len(set(minutes)) > 10
    assert sum(m < 2 for m in minutes) < len(minutes) / 4
    # Every slot still respects the windows.
    for s in slots:
        lo = local(s)
        assert 7 <= lo.hour < 10 or 18 <= lo.hour < 22, lo


def test_the_spread_keeps_a_short_window_mostly_free():
    """A quarter of the window at most: 20 minutes leave a 5-minute spread."""
    p = build_policy(
        interval_min_minutes=30, interval_max_minutes=30, jitter_seconds=0,
        windows=[{"start": 600, "end": 620}],  # 10:00-10:20
    )
    for seed in range(50):
        slot = compute_next_slot(now=madrid(2026, 6, 10, 4, 0), last_post_at=None,
                                 policy=p, rng=random.Random(seed))
        assert madrid(2026, 6, 10, 10, 0) <= slot <= madrid(2026, 6, 10, 10, 5)


def test_a_full_day_without_windows_does_not_restart_at_midnight_sharp():
    p = build_policy(interval_min_minutes=30, interval_max_minutes=30, jitter_seconds=0,
                     max_per_day=1)
    counts = {date(2026, 6, 10): 1}
    slot = compute_next_slot(now=madrid(2026, 6, 10, 15, 0), last_post_at=None, policy=p,
                             day_counter=lambda d: counts.get(d, 0),
                             rng=random.Random(3))
    assert madrid(2026, 6, 11, 0, 0) < slot <= madrid(2026, 6, 11, 0, 30)


def test_the_spread_still_keeps_the_account_separation():
    """A later opening hour is re-checked against the posts around it."""
    p = build_policy(
        interval_min_minutes=30, interval_max_minutes=30, jitter_seconds=0,
        windows=[{"start": 420, "end": 600}],
    )
    taken = [madrid(2026, 6, 10, 7, m) for m in range(0, 31, 10)]
    for seed in range(30):
        slot = compute_next_slot(now=madrid(2026, 6, 10, 4, 0), last_post_at=None, policy=p,
                                 spacing=Spacing.of(15, taken), rng=random.Random(seed))
        assert all(abs(slot - t) >= timedelta(minutes=15) for t in taken), local(slot)


def test_slot_after_window_rolls_to_next_morning():
    p = build_policy(
        interval_min_minutes=30, interval_max_minutes=30, jitter_seconds=0,
        windows=[{"start": 360, "end": 1260}],  # 06:00-21:00
    )
    now = madrid(2026, 6, 10, 22, 30)
    slot = compute_next_slot(now=now, last_post_at=None, policy=p)
    lo = local(slot)
    assert (lo.date(), lo.hour) == (date(2026, 6, 11), 6)


def test_slot_inside_window_is_kept():
    p = build_policy(
        interval_min_minutes=30, interval_max_minutes=30, jitter_seconds=0,
        windows=[{"start": 360, "end": 1260}],
    )
    now = madrid(2026, 6, 10, 14, 12)
    slot = compute_next_slot(now=now, last_post_at=None, policy=p)
    assert local(slot).hour == 14 and local(slot).minute == 12


@pytest.mark.parametrize(
    "hour,expected_hour,expected_day",
    [
        (23, 23, 10),   # inside the wrapping window, same evening
        (2, 2, 10),     # inside the tail of yesterday's window
        (5, 5, 10),     # still inside the tail
        (7, 22, 10),    # after the tail -> tonight's start
        (21, 22, 10),   # just before -> tonight's start
    ],
)
def test_window_that_wraps_midnight(hour, expected_hour, expected_day):
    p = build_policy(
        interval_min_minutes=30, interval_max_minutes=30, jitter_seconds=0,
        windows=[{"start": 1320, "end": 360}],  # 22:00 -> 06:00
    )
    now = madrid(2026, 6, 10, hour, 0)
    slot = compute_next_slot(now=now, last_post_at=None, policy=p)
    lo = local(slot)
    assert (lo.hour, lo.day) == (expected_hour, expected_day)


def test_multiple_windows_pick_the_nearest_start():
    p = build_policy(
        interval_min_minutes=30, interval_max_minutes=30, jitter_seconds=0,
        # 07:00-10:00 and 18:00-23:00
        windows=[{"start": 420, "end": 600}, {"start": 1080, "end": 1380}],
    )
    now = madrid(2026, 6, 10, 12, 0)
    slot = compute_next_slot(now=now, last_post_at=None, policy=p)
    assert local(slot).hour == 18

    now2 = madrid(2026, 6, 10, 5, 0)
    slot2 = compute_next_slot(now=now2, last_post_at=None, policy=p)
    assert local(slot2).hour == 7


def test_jitter_never_pushes_past_the_window_end():
    p = build_policy(
        interval_min_minutes=1, interval_max_minutes=1, min_gap_minutes=0,
        jitter_seconds=3600,
        windows=[{"start": 360, "end": 1260}],
    )
    last = madrid(2026, 6, 10, 20, 59, 30)
    now = last
    for seed in range(200):
        slot = compute_next_slot(
            now=now, last_post_at=last, policy=p, rng=random.Random(seed)
        )
        lo = local(slot)
        # Either clamped inside today's window, or rolled to tomorrow 06:00+.
        assert lo < datetime(2026, 6, 10, 21, 0, tzinfo=MADRID) or lo.day == 11


# --------------------------------------------------------------------------- #
# Daily cap
# --------------------------------------------------------------------------- #

def test_daily_cap_pushes_to_next_day():
    p = build_policy(
        interval_min_minutes=30, interval_max_minutes=30, jitter_seconds=0, max_per_day=3
    )
    now = madrid(2026, 6, 10, 12, 0)
    slot = compute_next_slot(
        now=now, last_post_at=None, policy=p,
        day_counter=lambda d: 3 if d == date(2026, 6, 10) else 0,
    )
    lo = local(slot)
    assert lo.date() == date(2026, 6, 11) and lo.hour == 0


def test_daily_cap_zero_means_no_slot():
    p = build_policy(interval_min_minutes=30, interval_max_minutes=30, max_per_day=0)
    assert compute_next_slot(now=madrid(2026, 6, 10, 12, 0), last_post_at=None, policy=p) is None


def test_no_daily_cap_allows_many_posts_in_one_day():
    p = build_policy(
        interval_min_minutes=30, interval_max_minutes=30, jitter_seconds=0, max_per_day=None
    )
    slots = preview_slots(
        now=madrid(2026, 6, 10, 0, 0), last_post_at=None, policy=p, count=20
    )
    assert len(slots) == 20
    same_day = [s for s in slots if local(s).date() == date(2026, 6, 10)]
    assert len(same_day) > 10


def test_cap_combined_with_window_rolls_to_next_window_start():
    p = build_policy(
        interval_min_minutes=30, interval_max_minutes=30, jitter_seconds=0,
        max_per_day=2, windows=[{"start": 600, "end": 1320}],  # 10:00-22:00
    )
    now = madrid(2026, 6, 10, 12, 0)
    slot = compute_next_slot(
        now=now, last_post_at=None, policy=p,
        day_counter=lambda d: 2 if d == date(2026, 6, 10) else 0,
    )
    lo = local(slot)
    assert (lo.date(), lo.hour) == (date(2026, 6, 11), 10)


# --------------------------------------------------------------------------- #
# Weekdays
# --------------------------------------------------------------------------- #

def test_disabled_weekday_is_skipped():
    # 2026-06-13 is a Saturday, 2026-06-14 a Sunday.
    p = build_policy(
        interval_min_minutes=30, interval_max_minutes=30, jitter_seconds=0,
        days_of_week=[0, 1, 2, 3, 4],  # weekdays only
    )
    now = madrid(2026, 6, 13, 10, 0)
    slot = compute_next_slot(now=now, last_post_at=None, policy=p)
    lo = local(slot)
    assert lo.date() == date(2026, 6, 15) and lo.weekday() == 0


# --------------------------------------------------------------------------- #
# Preview / simulation
# --------------------------------------------------------------------------- #

def test_preview_respects_cap_across_simulated_days():
    p = build_policy(
        interval_min_minutes=60, interval_max_minutes=60, jitter_seconds=0,
        max_per_day=3, windows=[{"start": 360, "end": 1260}],
    )
    slots = preview_slots(
        now=madrid(2026, 6, 10, 6, 0), last_post_at=None, policy=p, count=9
    )
    assert len(slots) == 9
    per_day: dict[date, int] = {}
    for s in slots:
        per_day[local(s).date()] = per_day.get(local(s).date(), 0) + 1
    assert set(per_day.values()) == {3}
    assert len(per_day) == 3


def test_preview_is_strictly_increasing():
    p = build_policy(
        interval_min_minutes=25, interval_max_minutes=35, max_per_day=8,
        windows=[{"start": 360, "end": 1260}],
    )
    slots = preview_slots(
        now=madrid(2026, 6, 10, 6, 0), last_post_at=None, policy=p, count=30,
        rng=random.Random(7),
    )
    assert slots == sorted(slots)
    assert len(set(slots)) == len(slots)


def test_preview_all_slots_inside_windows_and_allowed_days():
    p = build_policy(
        interval_min_minutes=25, interval_max_minutes=35, max_per_day=5,
        windows=[{"start": 1320, "end": 360}],  # 22:00-06:00
        days_of_week=[0, 1, 2, 3, 4, 5, 6],
    )
    slots = preview_slots(
        now=madrid(2026, 6, 10, 9, 0), last_post_at=None, policy=p, count=25,
        rng=random.Random(3),
    )
    assert len(slots) == 25
    for s in slots:
        h = local(s).hour
        assert h >= 22 or h < 6, f"slot outside window: {local(s)}"


def test_preview_returns_empty_when_policy_is_unsatisfiable():
    p = build_policy(interval_min_minutes=30, interval_max_minutes=30, max_per_day=0)
    assert preview_slots(
        now=madrid(2026, 6, 10, 12, 0), last_post_at=None, policy=p, count=5
    ) == []


# --------------------------------------------------------------------------- #
# DST — Europe/Madrid switches on the last Sunday of March / October
# --------------------------------------------------------------------------- #

def test_spring_forward_produces_valid_increasing_instants():
    # 2026-03-29: 02:00 -> 03:00 local. 02:30 does not exist.
    p = build_policy(
        interval_min_minutes=20, interval_max_minutes=20, jitter_seconds=0,
        windows=[{"start": 60, "end": 300}],  # 01:00 - 05:00
    )
    slots = preview_slots(
        now=madrid(2026, 3, 29, 0, 30), last_post_at=None, policy=p, count=8
    )
    assert slots == sorted(slots)
    for s in slots:
        lo = local(s)
        # 02:00-02:59 local simply does not exist on this date.
        assert not (lo.date() == date(2026, 3, 29) and lo.hour == 2)


def test_autumn_fall_back_does_not_duplicate_or_stall():
    # 2026-10-25: 03:00 -> 02:00 local, so 02:00-03:00 happens twice.
    p = build_policy(
        interval_min_minutes=20, interval_max_minutes=20, jitter_seconds=0,
        windows=[{"start": 60, "end": 300}],
    )
    slots = preview_slots(
        now=madrid(2026, 10, 25, 1, 0), last_post_at=None, policy=p, count=10
    )
    assert slots == sorted(slots)
    assert len(set(slots)) == len(slots)
    for a, b in zip(slots, slots[1:]):
        assert (b - a) >= timedelta(minutes=19)


def test_daily_cap_uses_local_calendar_days_not_utc():
    # In summer Madrid is UTC+2, so 00:30 local on the 11th is 22:30 UTC on the 10th.
    p = build_policy(
        interval_min_minutes=30, interval_max_minutes=30, jitter_seconds=0, max_per_day=1
    )
    now = madrid(2026, 6, 11, 0, 30)
    counted: list[date] = []

    def counter(d: date) -> int:
        counted.append(d)
        return 0

    compute_next_slot(now=now, last_post_at=None, policy=p, day_counter=counter)
    assert counted[0] == date(2026, 6, 11), "cap must be counted in Madrid days"


# --------------------------------------------------------------------------- #
# The account-wide separation
# --------------------------------------------------------------------------- #
#
# Several queues of one TikTok account are planned one after another against
# the same set of already-booked instants. The rule is a distance to the
# *neighbouring* posts: a queue is never made to wait for the end of what
# another queue booked, only to keep away from whatever sits next to the hour
# it wants.

def spacing_at(gap: int, *offsets_minutes: int, base: datetime | None = None) -> Spacing:
    base = base or madrid(2026, 6, 10, 12, 0)
    return Spacing.of(gap, [base + timedelta(minutes=m) for m in offsets_minutes])


def test_spacing_clears_a_candidate_that_is_too_close():
    base = madrid(2026, 6, 10, 12, 0)
    assert spacing_at(15, 5).clear_from(base) == base + timedelta(minutes=20)


def test_spacing_leaves_a_candidate_that_is_already_far_enough():
    base = madrid(2026, 6, 10, 12, 0)
    for offsets in ((15,), (-15,), (120, 300), (-600,)):
        assert spacing_at(15, *offsets).clear_from(base) == base, offsets


def test_spacing_walks_a_run_of_bookings():
    """Jumping past one obstacle may land on the next; all of them are cleared."""
    base = madrid(2026, 6, 10, 12, 0)
    booked = spacing_at(15, 5, 25, 45)
    assert booked.clear_from(base) == base + timedelta(minutes=60)


def test_a_hole_between_two_bookings_is_used():
    base = madrid(2026, 6, 10, 12, 0)
    assert spacing_at(15, 5, 120).clear_from(base) == base + timedelta(minutes=20)


def test_no_gap_configured_means_no_obstacles():
    base = madrid(2026, 6, 10, 12, 0)
    free = Spacing.of(0, [base, base + timedelta(minutes=1)])
    assert free.clear_from(base) == base and free.ceiling_for(base) is None


def test_a_slot_is_pushed_past_what_another_queue_owns():
    p = build_policy(interval_min_minutes=60, interval_max_minutes=60, jitter_seconds=0)
    now = madrid(2026, 6, 10, 12, 0)
    slot = compute_next_slot(
        now=now, last_post_at=None, policy=p, spacing=spacing_at(15, 5, base=now)
    )
    assert slot == now + timedelta(minutes=20)


def test_a_booking_far_in_the_future_does_not_delay_today():
    """The regression: a queue booked a fortnight ahead used to block the rest.

    The other queue of the account owns every hour up to two weeks from now;
    the hour being planned is nowhere near any of them, so it happens today.
    """
    p = build_policy(interval_min_minutes=60, interval_max_minutes=60, jitter_seconds=0)
    now = madrid(2026, 6, 10, 12, 0)
    booked = spacing_at(15, *range(240, 20_160, 180), base=now)  # every 3h for 14 days
    slot = compute_next_slot(now=now, last_post_at=None, policy=p, spacing=booked)
    assert slot == now, "el hueco de hoy estaba libre"


def test_two_queues_interleave_inside_one_day():
    """One queue owns 10:00, 13:00 and 16:00; the other fits in between.

    Not after them: the same afternoon, in the holes the first queue left.
    """
    p = build_policy(
        interval_min_minutes=90, interval_max_minutes=90, jitter_seconds=0,
        windows=[{"start": 600, "end": 1320}],
    )
    booked = [madrid(2026, 6, 10, h, 0) for h in (10, 13, 16)]
    slots = preview_slots(
        now=madrid(2026, 6, 10, 9, 0), last_post_at=None, policy=p, count=3,
        spacing=Spacing.of(30, booked),
    )
    assert len(slots) == 3
    assert all(s.astimezone(MADRID).date() == date(2026, 6, 10) for s in slots), \
        "el mismo día, no al final de lo que ocupaba la otra cola"
    assert all(s < booked[-1] for s in slots), [local(s) for s in slots]
    for slot in slots:
        nearest = min(abs((slot - b).total_seconds()) / 60 for b in booked)
        assert nearest >= 30, (local(slot), nearest)


def test_jitter_stops_short_of_the_next_booking():
    """An hour of jitter must not nudge a slot on top of the neighbour."""
    p = build_policy(
        interval_min_minutes=1, interval_max_minutes=1, jitter_seconds=3600,
    )
    now = madrid(2026, 6, 10, 12, 0)
    booked = spacing_at(15, 20, base=now)
    for seed in range(100):
        slot = compute_next_slot(
            now=now, last_post_at=None, policy=p, spacing=booked, rng=random.Random(seed)
        )
        assert now <= slot <= now + timedelta(minutes=5), slot


def test_a_preview_keeps_its_own_slots_apart():
    """Each simulated slot is an obstacle for the ones after it."""
    p = build_policy(interval_min_minutes=1, interval_max_minutes=1, jitter_seconds=0)
    now = madrid(2026, 6, 10, 12, 0)
    slots = preview_slots(
        now=now, last_post_at=None, policy=p, count=5, spacing=Spacing.of(45, []),
    )
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(slots, slots[1:])]
    assert gaps == [45, 45, 45, 45]


# --------------------------------------------------------------------------- #
# Advisory notes
# --------------------------------------------------------------------------- #

def test_describe_policy_warns_about_unreachable_cap():
    p = build_policy(
        interval_min_minutes=180, interval_max_minutes=240, max_per_day=20,
        windows=[{"start": 600, "end": 720}],  # only 2h a day
    )
    notes = " ".join(describe_policy(p))
    assert "máximo diario" in notes


def test_describe_policy_warns_about_aggressive_interval():
    p = build_policy(interval_min_minutes=3, interval_max_minutes=5)
    assert any("agresivo" in n for n in describe_policy(p))
