"""Deterministic expansion of local recurring maintenance windows into UTC.

Recurrences always advance along the *local wall-clock calendar*:

* DAILY  -> add ``interval`` calendar days to the naive local timestamp.
* WEEKLY -> stride by ``interval`` calendar weeks and emit the selected
  weekdays (ISO numbering, Monday=1 ... Sunday=7).

A naive local timestamp is then resolved to one UTC instant:

* unambiguous time                -> resolved directly;
* nonexistent wall time (spring-forward gap)
    -> moved forward by exactly the length of the timezone jump
       (equivalent to attaching the pre-transition offset, whose instant
       reads as the post-jump wall clock);
* ambiguous wall time (fall-back fold)
    -> ``earlier`` picks fold=0 (first occurrence), ``later`` picks fold=1.

The UTC end is ``utcStart + durationMinutes`` (pure UTC arithmetic, so a
window crossing a DST transition keeps the exact requested duration).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc

DAILY = "DAILY"
WEEKLY = "WEEKLY"

EARLIER = "earlier"
LATER = "later"

UNAMBIGUOUS = "UNAMBIGUOUS"
GAP_FORWARD_SHIFTED = "GAP_FORWARD_SHIFTED"
AMBIGUOUS_EARLIER = "AMBIGUOUS_EARLIER"
AMBIGUOUS_LATER = "AMBIGUOUS_LATER"

# Absolute hard cap: a request must never expand to more than this many
# recurrence instances (counted before query-range filtering).
MAX_EXPANSIONS = 10_000


@dataclass(frozen=True)
class Rule:
    id: int
    start_local: datetime
    duration_minutes: int
    frequency: str
    interval: int
    count: int | None
    until_local: datetime | None
    by_weekday: tuple[int, ...]


@dataclass(frozen=True)
class Window:
    rule_id: int
    occurrence: int
    frequency: str
    duration_minutes: int
    start_local: datetime          # original naive local wall time
    start_utc: datetime
    end_utc: datetime
    utc_offset: timedelta
    resolution: str


def _fold_instants(naive: datetime, tz: ZoneInfo):
    """Return both PEP 495 interpretations of a naive wall time.

    ``(utc_fold0, utc_fold1, back_fold0, back_fold1, offset0, offset1)``
    where ``back_*`` is the naive wall time reached by converting the
    instant back to the zone.
    """
    first = naive.replace(tzinfo=tz)
    second = naive.replace(tzinfo=tz, fold=1)
    u0 = first.astimezone(UTC)
    u1 = second.astimezone(UTC)
    back0 = u0.astimezone(tz).replace(tzinfo=None)
    back1 = u1.astimezone(tz).replace(tzinfo=None)
    return u0, u1, back0, back1, first.utcoffset(), second.utcoffset()


def resolve_local(
    naive: datetime, tz: ZoneInfo, ambiguous: str
) -> tuple[datetime, timedelta, str]:
    """Resolve a timezone-naive wall time to a UTC instant.

    Returns ``(utc_start, effective_utc_offset, resolution)``.
    """
    if naive.tzinfo is not None:
        raise ValueError("resolve_local expects a naive datetime")

    u0, u1, back0, back1, off0, off1 = _fold_instants(naive, tz)

    if back0 == naive and back1 == naive:
        if off0 == off1:
            return u0, off0, UNAMBIGUOUS
        # Both folds exist: the wall time happens twice.
        if ambiguous == EARLIER:
            return u0, off0, AMBIGUOUS_EARLIER
        return u1, off1, AMBIGUOUS_LATER

    # Nonexistent wall time (spring-forward gap). Attaching the
    # pre-transition offset (fold=0 in CPython's zoneinfo) yields exactly
    # the instant reached by moving the wall clock forward by the jump
    # length.
    utc_start = u0
    effective_offset = utc_start.astimezone(tz).utcoffset()
    return utc_start, effective_offset, GAP_FORWARD_SHIFTED


def _iter_local_occurrences(rule: Rule):
    """Yield recurrence instances as strictly ascending naive datetimes."""
    if rule.frequency == DAILY:
        i = 0
        while True:
            yield rule.start_local + timedelta(days=i * rule.interval)
            i += 1
        return

    # WEEKLY: anchor the first week on the Monday on/before start_local.
    anchor = date(
        rule.start_local.year, rule.start_local.month, rule.start_local.day
    )
    week_start = anchor - timedelta(days=anchor.weekday())  # Monday
    wall_time = time(
        hour=rule.start_local.hour,
        minute=rule.start_local.minute,
        second=rule.start_local.second,
        microsecond=rule.start_local.microsecond,
    )
    weekdays = sorted(rule.by_weekday)  # 1=Mon .. 7=Sun
    k = 0
    while True:
        for iso_wd in weekdays:
            day = week_start + timedelta(days=7 * k + (iso_wd - 1))
            candidate = datetime.combine(day, wall_time)
            if candidate >= rule.start_local:
                yield candidate
        k += rule.interval


def expand_rule(
    rule: Rule,
    tz: ZoneInfo,
    ambiguous: str,
    range_start_utc: datetime,
    range_end_utc: datetime,
    expansion_counter: list[int],
) -> list[Window]:
    windows: list[Window] = []
    emitted = 0

    # Canonical local wall time of the range end. A UTC instant never has a
    # wall representation inside a spring-forward gap, so this is always a
    # real (or ambiguous) wall time. Iteration bounds cannot be derived from
    # resolved UTC alone: under the gap forward-shift policy an occurrence
    # may map to a *later* UTC instant than the following plain days (the
    # wall clock was pushed forward, then fixed wall times run on DST).
    local_end = range_end_utc.astimezone(tz).replace(tzinfo=None)

    for naive in _iter_local_occurrences(rule):
        # Local-calendar termination conditions.
        if rule.count is not None and emitted >= rule.count:
            break
        if rule.until_local is not None and naive > rule.until_local:
            break

        # Count series instances before range filtering: the request is
        # rejected as soon as its full expansion exceeds the hard cap.
        emitted += 1
        expansion_counter[0] += 1
        if expansion_counter[0] > MAX_EXPANSIONS:
            raise ExpansionLimitExceeded(rule.id)

        if naive > local_end:
            # Wall time is past the end's wall time. With a positive-offset
            # transition this always means UTC-past-end, but on a fall-back
            # day the early (fold=0) interpretation of a slightly later wall
            # time can still precede range_end. Stop only once *both*
            # interpretations are at/after the end.
            u0, u1, _, _, _, _ = _fold_instants(naive, tz)
            if u0 >= range_end_utc and u1 >= range_end_utc:
                break

        start_utc, offset, resolution = resolve_local(naive, tz, ambiguous)

        # The query range filters on the UTC start only and is half-open.
        if start_utc < range_start_utc or start_utc >= range_end_utc:
            continue

        windows.append(
            Window(
                rule_id=rule.id,
                occurrence=emitted,
                frequency=rule.frequency,
                duration_minutes=rule.duration_minutes,
                start_local=naive,
                start_utc=start_utc,
                end_utc=start_utc + timedelta(minutes=rule.duration_minutes),
                utc_offset=offset,
                resolution=resolution,
            )
        )

    return windows


def expand(
    rules: list[Rule],
    tz: ZoneInfo,
    ambiguous: str,
    range_start_utc: datetime,
    range_end_utc: datetime,
) -> list[Window]:
    # The 10k cap applies to the request's expansion as a whole: reject as
    # soon as the summed series size crosses it, regardless of how narrow
    # the query range is. Computed in closed form before any timezone
    # resolution takes place.
    total_series = 0
    for rule in rules:
        size = series_size(rule)
        total_series += size
        if total_series > MAX_EXPANSIONS:
            raise ExpansionLimitExceeded(rule.id, total_series)

    counter = [0]
    all_windows: list[Window] = []
    for rule in rules:
        all_windows.extend(
            expand_rule(
                rule, tz, ambiguous, range_start_utc, range_end_utc, counter
            )
        )
        # Runtime defence in depth for the filtered expansion itself.
        if len(all_windows) > MAX_EXPANSIONS:
            raise ExpansionLimitExceeded(rule.id, len(all_windows))
    # Stable ordering: UTC start first, then the client-assigned rule id.
    all_windows.sort(key=lambda w: (w.start_utc, w.rule_id))
    return all_windows


class ExpansionLimitExceeded(Exception):
    def __init__(self, rule_id: int, total: int | None = None):
        if total is not None:
            super().__init__(
                f"rule {rule_id}: series expands to {total} instances "
                f"(limit {MAX_EXPANSIONS})"
            )
        else:
            super().__init__(
                f"rule {rule_id}: expansion would exceed {MAX_EXPANSIONS} instances"
            )
        self.rule_id = rule_id


def series_size(rule: Rule) -> int:
    """Total number of recurrence instances the series produces.

    This counts the *whole* local-calendar series (ignoring the query
    range), so the hard cap can be enforced deterministically and cheaply
    before any timezone resolution takes place.
    """
    if rule.count is not None:
        return rule.count

    assert rule.until_local is not None
    if rule.frequency == DAILY:
        days = (rule.until_local.date() - rule.start_local.date()).days
        if days < 0 or rule.until_local < rule.start_local:
            return 0
        # Same wall-time comparison on the final day is already guaranteed
        # because all daily instances share the wall time of start_local.
        return days // rule.interval + 1

    # WEEKLY: anchor on the Monday on/before start_local, then count, per
    # selected weekday d (1..7), the weeks k = 0, interval, 2*interval, ...
    # whose day 7k + (d-1) falls inside [start_date, until_date].
    anchor = date(
        rule.start_local.year, rule.start_local.month, rule.start_local.day
    ) - timedelta(days=rule.start_local.isoweekday() - 1)
    d0 = (rule.start_local.date() - anchor).days
    d1 = (rule.until_local.date() - anchor).days
    wall = rule.start_local.time()
    if rule.until_local.time() < wall:
        d1 -= 1  # final day's occurrence would be later than until
    if d1 < d0:
        return 0

    total = 0
    for iso_wd in rule.by_weekday:
        off = iso_wd - 1  # 0..6 within the anchor week
        lo = (d0 - off + 6) // 7   # ceil, may be negative
        hi = (d1 - off) // 7       # floor
        if hi < max(lo, 0):
            continue
        lo = max(lo, 0)
        first = ((lo + rule.interval - 1) // rule.interval) * rule.interval
        last = (hi // rule.interval) * rule.interval
        if last >= first:
            total += (last - first) // rule.interval + 1
    return total
