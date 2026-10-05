"""Expansion of local recurring maintenance windows into UTC intervals.

All recurrence arithmetic advances along the *local civil calendar* of the
configured IANA timezone; only the resulting instants are converted to UTC.

DST handling rules (per occurrence):
  * normal wall time            -> resolved directly;
  * nonexistent wall time       -> shifted forward by exactly the length of
                                   the timezone jump (spring-forward gap);
  * repeated (ambiguous) time   -> the "earlier" or "later" UTC instant is
                                   chosen according to the request policy.

The end of a window is always the resolved UTC start plus ``durationMinutes``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, List, Optional, Tuple
from zoneinfo import ZoneInfo

MAX_TOTAL_OCCURRENCES = 10_000

WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

UTC = timezone.utc


class ExpansionError(Exception):
    """A deterministic, caller-reportable expansion failure."""

    def __init__(self, code: str, message: str, rule_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.rule_id = rule_id


@dataclass(frozen=True)
class Rule:
    id: str
    start_local: datetime  # naive local wall time
    duration_minutes: int
    frequency: str  # "DAILY" | "WEEKLY"
    interval: int
    weekdays: Optional[Tuple[str, ...]]  # WEEKLY only
    count: Optional[int]
    until: Optional[datetime]  # naive local wall time, inclusive


@dataclass(frozen=True)
class Window:
    rule_id: str
    local_start: datetime  # original local occurrence (naive)
    utc_start: datetime
    utc_end: datetime
    utc_offset: timedelta
    resolution: str  # "normal" | "ambiguous-earlier" | "ambiguous-later" | "gap-shifted"


def _roundtrips(aware: datetime, tz: ZoneInfo, naive: datetime) -> bool:
    """True if ``aware`` maps back to the wall time ``naive`` in ``tz``."""
    return aware.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == naive


def _scan_offset(naive: datetime, tz: ZoneInfo, step: timedelta) -> timedelta:
    """UTC offset of the nearest valid wall time in the direction of ``step``."""
    t = naive
    for _ in range(96):  # up to 48h in 30-minute steps (covers dateline jumps)
        t = t + step
        candidate = t.replace(tzinfo=tz)
        if _roundtrips(candidate, tz, t):
            offset = candidate.utcoffset()
            if offset is not None:
                return offset
    raise ExpansionError(
        "UNRESOLVABLE_LOCAL_TIME",
        f"cannot determine timezone transition around local time {naive.isoformat()}",
    )


def resolve_local(naive: datetime, tz: ZoneInfo, policy: str) -> Tuple[datetime, str]:
    """Resolve a naive local wall time to an aware datetime in ``tz``.

    ``policy`` is "earlier" or "later" and only affects ambiguous (repeated)
    wall times. Nonexistent wall times are shifted forward by exactly the
    length of the timezone jump.
    """
    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    ok_first = _roundtrips(first, tz, naive)
    ok_second = _roundtrips(second, tz, naive)

    if ok_first and ok_second:
        if first.utcoffset() == second.utcoffset():
            return first, "normal"
        # Repeated wall time: two distinct valid UTC instants exist.
        earlier, later = sorted((first, second), key=lambda a: a.astimezone(UTC))
        if policy == "later":
            return later, "ambiguous-later"
        return earlier, "ambiguous-earlier"
    if ok_first:
        return first, "normal"
    if ok_second:
        return second, "normal"

    # Nonexistent wall time (spring-forward gap): move forward by exactly the
    # jump length, i.e. the difference between the offsets after and before
    # the gap.
    offset_before = _scan_offset(naive, tz, timedelta(minutes=-30))
    offset_after = _scan_offset(naive, tz, timedelta(minutes=30))
    gap = offset_after - offset_before
    shifted = naive + gap
    for _ in range(4):  # extremely defensive: re-apply if still inside a gap
        candidate = shifted.replace(tzinfo=tz)
        if _roundtrips(candidate, tz, shifted):
            return candidate, "gap-shifted"
        shifted = shifted + gap
    raise ExpansionError(
        "UNRESOLVABLE_LOCAL_TIME",
        f"local time {naive.isoformat()} does not exist and cannot be shifted forward",
    )


def _local_occurrences(rule: Rule) -> Iterator[datetime]:
    """Yield naive local occurrence times, advancing on the local calendar."""
    start = rule.start_local
    if rule.frequency == "DAILY":
        n = 0
        while True:
            yield start + timedelta(days=rule.interval * n)
            n += 1
    else:  # WEEKLY
        assert rule.weekdays, "WEEKLY rules must carry a weekday set"
        offsets = sorted(WEEKDAY_CODES.index(code) for code in rule.weekdays)
        anchor_monday = (start - timedelta(days=start.weekday())).date()
        week = 0
        while True:
            monday = anchor_monday + timedelta(weeks=rule.interval * week)
            for offset in offsets:
                occurrence = datetime.combine(monday + timedelta(days=offset), start.time())
                if occurrence >= start:
                    yield occurrence
            week += 1


def expand_rules(
    rules: List[Rule],
    tz: ZoneInfo,
    policy: str,
    range_start: datetime,
    range_end: datetime,
) -> List[Window]:
    """Expand ``rules`` into UTC windows, filtered to ``[range_start, range_end)``.

    Only the UTC *start* of a window is tested against the query range. The
    total number of generated occurrences across all rules is capped at
    ``MAX_TOTAL_OCCURRENCES``; exceeding it aborts the whole request.
    """
    windows: List[Window] = []
    budget = MAX_TOTAL_OCCURRENCES
    for rule in rules:
        produced = 0
        for local in _local_occurrences(rule):
            if rule.count is not None and produced >= rule.count:
                break
            if rule.until is not None and local > rule.until:
                break
            produced += 1
            budget -= 1
            if budget < 0:
                raise ExpansionError(
                    "EXPANSION_LIMIT_EXCEEDED",
                    f"rule {rule.id!r}: expansion exceeds {MAX_TOTAL_OCCURRENCES} items",
                    rule.id,
                )
            try:
                aware, resolution = resolve_local(local, tz, policy)
            except ExpansionError as exc:
                if exc.rule_id is None:
                    exc.rule_id = rule.id
                    exc.message = f"rule {rule.id!r}: {exc.message}"
                raise
            utc_start = aware.astimezone(UTC)
            utc_end = utc_start + timedelta(minutes=rule.duration_minutes)
            if range_start <= utc_start < range_end:
                windows.append(
                    Window(
                        rule_id=rule.id,
                        local_start=local,
                        utc_start=utc_start,
                        utc_end=utc_end,
                        utc_offset=aware.utcoffset() or timedelta(0),
                        resolution=resolution,
                    )
                )
    # Stable sort by UTC start, then rule id.
    windows.sort(key=lambda w: (w.utc_start, w.rule_id))
    return windows
