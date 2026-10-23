"""Pure scheduling maths.

No database, no network, no clock reads: everything is passed in. That makes the
whole "when does the next video go out?" logic exhaustively unit-testable, which
matters because it is the one part of the tool a user cannot eyeball.

All datetimes crossing this module's boundary are timezone-aware UTC.
Local-time reasoning (windows, daily caps, weekdays) happens in the account's
timezone, Europe/Madrid by default.
"""
from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MINUTES_PER_DAY = 24 * 60
# A slot further out than this means the policy is effectively unsatisfiable.
MAX_LOOKAHEAD_DAYS = 400
_MAX_ITERATIONS = MAX_LOOKAHEAD_DAYS * 8

# How far past the opening of a window (or of a day) a post may land when the
# planner had to wait for that opening. Without it a queue whose cadence fits
# between two windows would post at 07:00 and 18:00 on the dot, every day. It
# is capped at a quarter of what is left of the window, so a short window is
# never mostly spent waiting.
OPENING_SPREAD_MINUTES = 30

# The four "logical" cadences offered in the UI, plus free-form custom values.
INTERVAL_PRESETS: list[dict] = [
    {"id": "burst", "label": "Ráfaga", "min": 25, "max": 35,
     "hint": "≈2 vídeos/hora · para empujar una cuenta nueva"},
    {"id": "frequent", "label": "Frecuente", "min": 55, "max": 75,
     "hint": "≈1 vídeo/hora · ritmo alto sostenible"},
    {"id": "balanced", "label": "Equilibrado", "min": 150, "max": 210,
     "hint": "cada 2,5–3,5 h · el más seguro para cuentas consolidadas"},
    {"id": "slow", "label": "Pausado", "min": 300, "max": 420,
     "hint": "cada 5–7 h · 2–3 vídeos al día"},
]

# Ready-made hour windows for the UI (minutes from midnight, local time).
WINDOW_PRESETS: list[dict] = [
    {"id": "any", "label": "Indiferente", "windows": []},
    {"id": "day", "label": "06:00 – 21:00", "windows": [{"start": 360, "end": 1260}]},
    {"id": "primetime", "label": "10:00 – 22:00", "windows": [{"start": 600, "end": 1320}]},
    {"id": "night", "label": "22:00 – 06:00", "windows": [{"start": 1320, "end": 360}]},
    {"id": "peaks", "label": "Picos (07-10 y 18-23)",
     "windows": [{"start": 420, "end": 600}, {"start": 1080, "end": 1380}]},
]


class PolicyError(ValueError):
    """Raised when a schedule policy cannot possibly be satisfied."""


@dataclass(frozen=True)
class Window:
    """A daily time window, in minutes from local midnight.

    ``end <= start`` means the window wraps past midnight (e.g. 22:00 -> 06:00).
    ``start == end`` means the whole day.
    """

    start: int
    end: int

    def __post_init__(self) -> None:
        if not (0 <= self.start < MINUTES_PER_DAY):
            raise PolicyError(f"inicio de franja fuera de rango: {self.start}")
        if not (0 < self.end <= MINUTES_PER_DAY):
            raise PolicyError(f"fin de franja fuera de rango: {self.end}")

    @property
    def span_end(self) -> int:
        """End expressed on the same axis as ``start`` (may exceed 1440)."""
        if self.start == self.end:
            return self.start + MINUTES_PER_DAY
        if self.end <= self.start:
            return self.end + MINUTES_PER_DAY
        return self.end

    @property
    def wraps(self) -> bool:
        return self.span_end > MINUTES_PER_DAY

    @property
    def duration_minutes(self) -> int:
        return self.span_end - self.start


@dataclass(frozen=True)
class SchedulePolicy:
    interval_min_minutes: int = 55
    interval_max_minutes: int = 75
    min_gap_minutes: int = 15
    jitter_seconds: int = 90
    max_per_day: int | None = None
    windows: tuple[Window, ...] = ()
    opening_spread_minutes: int = OPENING_SPREAD_MINUTES
    days_of_week: frozenset[int] = field(default_factory=lambda: frozenset(range(7)))
    timezone: str = "Europe/Madrid"

    def __post_init__(self) -> None:
        if self.interval_min_minutes < 1 or self.interval_max_minutes < 1:
            raise PolicyError("el intervalo debe ser de al menos 1 minuto")
        if self.interval_min_minutes > self.interval_max_minutes:
            raise PolicyError("el intervalo mínimo no puede superar al máximo")
        if self.max_per_day is not None and self.max_per_day < 0:
            raise PolicyError("el máximo diario no puede ser negativo")
        if self.jitter_seconds < 0:
            raise PolicyError("el jitter no puede ser negativo")
        if self.opening_spread_minutes < 0:
            raise PolicyError("el margen tras abrir la franja no puede ser negativo")
        if not self.days_of_week:
            raise PolicyError("hay que permitir al menos un día de la semana")
        if any(d not in range(7) for d in self.days_of_week):
            raise PolicyError("días de la semana inválidos (0=lunes … 6=domingo)")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise PolicyError(f"zona horaria desconocida: {self.timezone}") from exc

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def theoretical_max_per_day(self) -> float:
        """Upper bound on posts/day implied by the interval and the windows."""
        minutes_available = (
            sum(w.duration_minutes for w in self.windows) if self.windows else MINUTES_PER_DAY
        )
        return minutes_available / max(self.interval_min_minutes, 1)


def build_policy(
    *,
    interval_min_minutes: int,
    interval_max_minutes: int,
    min_gap_minutes: int = 15,
    jitter_seconds: int = 90,
    max_per_day: int | None = None,
    windows: list[dict] | tuple[Window, ...] | None = None,
    days_of_week: list[int] | None = None,
    timezone_name: str = "Europe/Madrid",
    opening_spread_minutes: int = OPENING_SPREAD_MINUTES,
) -> SchedulePolicy:
    """Build a policy from loosely-typed (JSON / form) input."""
    win: tuple[Window, ...] = ()
    if windows:
        parsed = []
        for w in windows:
            if isinstance(w, Window):
                parsed.append(w)
            else:
                parsed.append(Window(int(w["start"]), int(w["end"])))
        win = tuple(_merge_windows(parsed))
    return SchedulePolicy(
        interval_min_minutes=int(interval_min_minutes),
        interval_max_minutes=int(interval_max_minutes),
        min_gap_minutes=int(min_gap_minutes),
        jitter_seconds=int(jitter_seconds),
        max_per_day=None if max_per_day is None else int(max_per_day),
        windows=win,
        days_of_week=frozenset(days_of_week if days_of_week is not None else range(7)),
        timezone=timezone_name,
        opening_spread_minutes=int(opening_spread_minutes),
    )


def _merge_windows(windows: list[Window]) -> list[Window]:
    """Drop windows fully covered by another; collapse a 24h cover to 'any time'.

    Overlapping-but-distinct windows are kept as-is: the containment test below
    handles overlap correctly, so merging them is an optimisation, not a need.
    """
    if not windows:
        return []
    if any(w.duration_minutes >= MINUTES_PER_DAY for w in windows):
        return []  # a full-day window is the same as "indiferente"
    unique: list[Window] = []
    for w in windows:
        if w not in unique:
            unique.append(w)
    return sorted(unique, key=lambda w: (w.start, w.span_end))


# --------------------------------------------------------------------------- #
# Local-time helpers
# --------------------------------------------------------------------------- #

def _at_local_minutes(tz: ZoneInfo, day: date, minutes: int) -> datetime:
    """Local ``day`` midnight + ``minutes``, returned as aware UTC.

    ``minutes`` may exceed 1440 to reach into the following day. If the wall
    clock time does not exist (DST spring-forward) zoneinfo resolves it to a
    real instant just after the gap, which is exactly what we want.
    """
    naive = datetime.combine(day, time(0, 0)) + timedelta(minutes=minutes)
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)


def _next_local_day_start(moment_utc: datetime, tz: ZoneInfo) -> datetime:
    local_day = moment_utc.astimezone(tz).date()
    return _at_local_minutes(tz, local_day + timedelta(days=1), 0)


def _window_bounds(tz: ZoneInfo, day: date, window: Window) -> tuple[datetime, datetime]:
    return (
        _at_local_minutes(tz, day, window.start),
        _at_local_minutes(tz, day, window.span_end),
    )


def _containing_window_end(
    moment_utc: datetime, windows: tuple[Window, ...], tz: ZoneInfo
) -> datetime | None:
    """End instant of the window containing ``moment_utc``, or None."""
    today = moment_utc.astimezone(tz).date()
    # Yesterday's anchor matters for windows that wrap past midnight.
    for day in (today - timedelta(days=1), today):
        for window in windows:
            start, end = _window_bounds(tz, day, window)
            if start <= moment_utc < end:
                return end
    return None


def _next_window_start(
    moment_utc: datetime, windows: tuple[Window, ...], tz: ZoneInfo
) -> datetime | None:
    today = moment_utc.astimezone(tz).date()
    candidates = [
        _window_bounds(tz, today + timedelta(days=offset), window)[0]
        for offset in (0, 1, 2)
        for window in windows
    ]
    future = [c for c in candidates if c > moment_utc]
    return min(future) if future else None


# --------------------------------------------------------------------------- #
# The scheduler itself
# --------------------------------------------------------------------------- #

DayCounter = Callable[[date], int]


def _no_posts(_day: date) -> int:
    return 0


@dataclass(frozen=True)
class DayLimit:
    """A daily ceiling counted outside this policy.

    Used for the account-wide cap: several queues of the same TikTok account
    each have their own ``max_per_day``, but together they must also stay under
    what the account as a whole is allowed to post.
    """

    max_per_day: int | None
    counter: DayCounter = _no_posts

    def is_full(self, day: date) -> bool:
        return self.max_per_day is not None and self.counter(day) >= self.max_per_day


@dataclass(frozen=True)
class Spacing:
    """Instants already taken, and how far a new post must stay from each.

    This is the account-wide "separación mínima". It is a distance to the
    *neighbouring* posts, not a queue to stand at the end of: every queue of a
    TikTok account plans against the same set of taken instants, so a second
    queue weaves between what the first one booked instead of starting after
    the last of it.
    """

    gap_minutes: int = 0
    # Ascending UTC. Only instants near the horizon being planned are worth
    # passing in: anything further back than the gap can never conflict.
    taken: tuple[datetime, ...] = ()

    @classmethod
    def of(cls, gap_minutes: int, instants: Iterable[datetime]) -> "Spacing":
        gap = max(int(gap_minutes or 0), 0)
        if not gap:
            return cls()
        return cls(gap, tuple(sorted(i.astimezone(timezone.utc) for i in instants)))

    @property
    def gap(self) -> timedelta:
        return timedelta(minutes=self.gap_minutes)

    def clear_from(self, candidate: datetime) -> datetime:
        """``candidate``, pushed forward until it clears every taken instant."""
        if not self.gap_minutes:
            return candidate
        gap = self.gap
        for stamp in self.taken:
            if stamp + gap <= candidate:
                continue  # far enough behind
            if candidate + gap <= stamp:
                break  # far enough ahead — and so is everything after it
            candidate = stamp + gap
        return candidate

    def ceiling_for(self, candidate: datetime) -> datetime | None:
        """How far an already-clear ``candidate`` may be nudged and stay clear."""
        if not self.gap_minutes:
            return None
        for stamp in self.taken:
            if stamp > candidate:
                return stamp - self.gap
        return None


NO_SPACING = Spacing()


def compute_next_slot(
    *,
    now: datetime,
    last_post_at: datetime | None,
    policy: SchedulePolicy,
    day_counter: DayCounter = _no_posts,
    extra_limits: Sequence[DayLimit] = (),
    spacing: Spacing = NO_SPACING,
    rng: random.Random | None = None,
) -> datetime | None:
    """Return the next allowed posting instant (aware UTC), or None.

    None means "no valid slot within the lookahead horizon" — e.g. max_per_day
    is 0, or every weekday is disabled.

    ``day_counter(local_date)`` must report how many posts are already committed
    (published *and* scheduled) on that local calendar day. ``extra_limits`` are
    further ceilings with their own counters, all of which must have room, and
    ``spacing`` keeps the result away from the instants the account already owns.
    """
    rng = rng or random.Random()
    tz = policy.tz
    now = now.astimezone(timezone.utc)

    if policy.max_per_day == 0 or any(limit.max_per_day == 0 for limit in extra_limits):
        return None

    # 1. Earliest instant allowed by the cadence rules.
    if last_post_at is None:
        candidate = now
    else:
        last_post_at = last_post_at.astimezone(timezone.utc)
        gap = rng.uniform(policy.interval_min_minutes, policy.interval_max_minutes)
        gap = max(gap, policy.min_gap_minutes)
        candidate = last_post_at + timedelta(minutes=gap)
    if candidate < now:
        candidate = now

    # 2. Walk forward until the candidate satisfies day / cap / window rules.
    # ``opening`` says the candidate was moved onto the start of a window or a
    # day: an hour every waiting queue would otherwise pick, day after day.
    opening = False
    for _ in range(_MAX_ITERATIONS):
        # Separation first: a candidate too close to a post the account already
        # owns jumps past it and is re-checked from the top, so the jump can
        # never land outside a window or on a day that is already full.
        spaced = spacing.clear_from(candidate)
        if spaced > candidate:
            candidate = spaced
            opening = False
            if candidate > now + timedelta(days=MAX_LOOKAHEAD_DAYS):
                return None
            continue

        local_day = candidate.astimezone(tz).date()

        if local_day.weekday() not in policy.days_of_week:
            candidate = _next_local_day_start(candidate, tz)
            opening = True
            continue

        day_full = policy.max_per_day is not None and day_counter(local_day) >= policy.max_per_day
        if day_full or any(limit.is_full(local_day) for limit in extra_limits):
            candidate = _next_local_day_start(candidate, tz)
            opening = True
            continue

        window_end = (
            _containing_window_end(candidate, policy.windows, tz) if policy.windows else None
        )
        if not policy.windows or window_end is not None:
            if opening:
                # Spread it into the window, then check everything again: the
                # later hour may sit too close to a post or past midnight.
                candidate = _spread_opening(candidate, policy, rng, window_end)
                opening = False
                continue
            return _apply_jitter(candidate, policy, rng, now, window_end, spacing)

        nxt = _next_window_start(candidate, policy.windows, tz)
        if nxt is None or nxt <= candidate:
            # Defensive: never stall. Should be unreachable with valid windows.
            nxt = candidate + timedelta(minutes=1)
        candidate = nxt
        opening = True

        if candidate > now + timedelta(days=MAX_LOOKAHEAD_DAYS):
            return None

    return None


def _spread_opening(
    candidate: datetime,
    policy: SchedulePolicy,
    rng: random.Random,
    window_end: datetime | None,
) -> datetime:
    """Move a slot that would sit on a window's (or day's) opening a little later.

    Up to ``opening_spread_minutes``, and never more than a quarter of what is
    left of the window, so the post stays well inside it.
    """
    cap = float(policy.opening_spread_minutes)
    if window_end is not None:
        cap = min(cap, (window_end - candidate).total_seconds() / 60 / 4)
    if cap <= 0:
        return candidate
    return candidate + timedelta(minutes=rng.uniform(0, cap))


def _apply_jitter(
    candidate: datetime,
    policy: SchedulePolicy,
    rng: random.Random,
    now: datetime,
    window_end: datetime | None,
    spacing: Spacing = NO_SPACING,
) -> datetime:
    """Nudge the slot forward a little so posts never land on round numbers.

    Jitter is forward-only: a negative nudge could push the slot before ``now``
    or violate the minimum gap we just enforced. For the same reason it stops
    short of the next post the account already owns — the ceiling is read from
    the un-nudged candidate, which the caller has just made clear.
    """
    ceiling = spacing.ceiling_for(candidate)
    if policy.jitter_seconds > 0:
        candidate = candidate + timedelta(seconds=rng.randint(0, policy.jitter_seconds))
    if window_end is not None and candidate >= window_end:
        candidate = window_end - timedelta(seconds=1)
    if ceiling is not None and candidate > ceiling:
        candidate = ceiling
    return max(candidate, now)


def preview_slots(
    *,
    now: datetime,
    last_post_at: datetime | None,
    policy: SchedulePolicy,
    count: int = 10,
    day_counter: DayCounter = _no_posts,
    extra_limits: Sequence[DayLimit] = (),
    spacing: Spacing = NO_SPACING,
    rng: random.Random | None = None,
) -> list[datetime]:
    """Simulate the next ``count`` slots, honouring the daily cap as it fills up.

    Used by the UI's "ver próximas publicaciones" preview so the user can sanity
    check a configuration before enabling it.
    """
    rng = rng or random.Random()
    tz = policy.tz
    simulated: dict[date, int] = defaultdict(int)

    def with_simulated(base: DayCounter) -> DayCounter:
        # Each simulated post also fills the account-wide budget, or a preview
        # would happily show 12 slots that the real scheduler will never grant.
        return lambda day: base(day) + simulated[day]

    counter = with_simulated(day_counter)
    limits = tuple(
        DayLimit(limit.max_per_day, with_simulated(limit.counter)) for limit in extra_limits
    )

    slots: list[datetime] = []
    cursor_now = now
    last = last_post_at
    # Each simulated slot becomes an obstacle for the next ones, exactly as the
    # real booking would once the preview is committed.
    taken = list(spacing.taken)
    for _ in range(count):
        slot = compute_next_slot(
            now=cursor_now, last_post_at=last, policy=policy, day_counter=counter,
            extra_limits=limits, spacing=Spacing.of(spacing.gap_minutes, taken), rng=rng,
        )
        if slot is None:
            break
        slots.append(slot)
        simulated[slot.astimezone(tz).date()] += 1
        taken.append(slot)
        last = slot
        cursor_now = slot
    return slots


def describe_policy(policy: SchedulePolicy) -> list[str]:
    """Human-readable warnings about a policy (shown in the UI, not fatal)."""
    notes: list[str] = []
    if policy.max_per_day is not None:
        reachable = policy.theoretical_max_per_day
        if policy.max_per_day > reachable:
            notes.append(
                f"El máximo diario ({policy.max_per_day}) es mayor que lo que permite "
                f"el intervalo dentro de tus franjas (~{reachable:.1f}/día). "
                "Mandará el intervalo, no el máximo."
            )
    if policy.interval_min_minutes < 10:
        notes.append(
            "Un intervalo por debajo de 10 minutos es muy agresivo y puede "
            "activar los límites antispam de TikTok."
        )
    if len(policy.days_of_week) < 7:
        missing = sorted(set(range(7)) - policy.days_of_week)
        names = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
        notes.append("Días desactivados: " + ", ".join(names[d] for d in missing) + ".")
    if policy.windows:
        total = sum(w.duration_minutes for w in policy.windows)
        if total < policy.interval_min_minutes:
            notes.append(
                "Las franjas horarias son más cortas que el intervalo mínimo: "
                "como mucho saldrá un vídeo por día."
            )
    return notes
