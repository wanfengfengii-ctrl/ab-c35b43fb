"""Unit tests for recurrence generation and DST-safe UTC resolution."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.expander import (
    ExpansionError,
    MAX_TOTAL_OCCURRENCES,
    Rule,
    expand_rules,
    resolve_local,
)

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")
TOKYO = ZoneInfo("Asia/Tokyo")
LORD_HOWE = ZoneInfo("Australia/Lord_Howe")

RANGE = (datetime(2020, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC))


def make_rule(**overrides) -> Rule:
    base = dict(
        id="r",
        start_local=datetime(2026, 1, 1, 9, 0),
        duration_minutes=60,
        frequency="DAILY",
        interval=1,
        weekdays=None,
        count=1,
        until=None,
    )
    base.update(overrides)
    return Rule(**base)


def expand(rule, tz=ZoneInfo("UTC"), policy="earlier", rng=RANGE):
    return expand_rules([rule], tz, policy, rng[0], rng[1])


# ---------------------------------------------------------------- DST gaps


def test_nonexistent_wall_time_shifts_forward_by_exact_gap():
    # US spring forward 2026-03-08: 02:00 -> 03:00, jump = 1 hour.
    aware, resolution = resolve_local(datetime(2026, 3, 8, 2, 30), NY, "earlier")
    assert resolution == "gap-shifted"
    assert aware.replace(tzinfo=None) == datetime(2026, 3, 8, 3, 30)  # +1h exactly
    assert aware.utcoffset() == timedelta(hours=-4)
    assert aware.astimezone(UTC) == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)


def test_gap_shift_uses_exact_jump_for_half_hour_transitions():
    # Lord Howe spring forward 2026-10-04: 02:00 -> 02:30, jump = 30 minutes.
    aware, resolution = resolve_local(datetime(2026, 10, 4, 2, 15), LORD_HOWE, "earlier")
    assert resolution == "gap-shifted"
    assert aware.replace(tzinfo=None) == datetime(2026, 10, 4, 2, 45)  # +30m exactly
    assert aware.utcoffset() == timedelta(hours=11)


def test_wall_time_just_before_gap_is_untouched():
    aware, resolution = resolve_local(datetime(2026, 3, 8, 1, 59, 59), NY, "earlier")
    assert resolution == "normal"
    assert aware.astimezone(UTC) == datetime(2026, 3, 8, 6, 59, 59, tzinfo=UTC)


# ------------------------------------------------------- ambiguous times


def test_ambiguous_wall_time_policy_earlier():
    # US fall back 2026-11-01: 01:30 happens twice (EDT then EST).
    aware, resolution = resolve_local(datetime(2026, 11, 1, 1, 30), NY, "earlier")
    assert resolution == "ambiguous-earlier"
    assert aware.utcoffset() == timedelta(hours=-4)
    assert aware.astimezone(UTC) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


def test_ambiguous_wall_time_policy_later():
    aware, resolution = resolve_local(datetime(2026, 11, 1, 1, 30), NY, "later")
    assert resolution == "ambiguous-later"
    assert aware.utcoffset() == timedelta(hours=-5)
    assert aware.astimezone(UTC) == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


def test_normal_wall_time():
    aware, resolution = resolve_local(datetime(2026, 1, 15, 9, 0), NY, "earlier")
    assert resolution == "normal"
    assert aware.astimezone(UTC) == datetime(2026, 1, 15, 14, 0, tzinfo=UTC)


# ------------------------------------------------------- recurrence rules


def test_daily_recurrence_advances_on_local_calendar_across_dst():
    rule = make_rule(start_local=datetime(2026, 3, 6, 9, 0), interval=2, count=4)
    windows = expand(rule, tz=NY)
    # Local wall time stays 09:00 even though the UTC offset changes on 03-08.
    assert [w.local_start for w in windows] == [
        datetime(2026, 3, 6, 9, 0),
        datetime(2026, 3, 8, 9, 0),
        datetime(2026, 3, 10, 9, 0),
        datetime(2026, 3, 12, 9, 0),
    ]
    assert [w.utc_start.hour for w in windows] == [14, 13, 13, 13]


def test_weekly_generation_skips_days_before_start_in_first_week():
    # 2026-03-04 is a Wednesday.
    rule = make_rule(
        start_local=datetime(2026, 3, 4, 9, 0),
        frequency="WEEKLY",
        weekdays=("MO", "WE"),
        count=4,
    )
    windows = expand(rule)
    assert [w.local_start for w in windows] == [
        datetime(2026, 3, 4, 9, 0),   # Wed of the anchor week (== start)
        datetime(2026, 3, 9, 9, 0),   # Mon of week +1
        datetime(2026, 3, 11, 9, 0),  # Wed of week +1
        datetime(2026, 3, 16, 9, 0),  # Mon of week +2
    ]


def test_weekly_interval_steps_whole_weeks():
    # 2026-01-05 is a Monday.
    rule = make_rule(
        start_local=datetime(2026, 1, 5, 10, 0),
        frequency="WEEKLY",
        interval=2,
        weekdays=("MO", "FR"),
        count=4,
    )
    windows = expand(rule)
    assert [w.local_start for w in windows] == [
        datetime(2026, 1, 5, 10, 0),
        datetime(2026, 1, 9, 10, 0),
        datetime(2026, 1, 19, 10, 0),
        datetime(2026, 1, 23, 10, 0),
    ]


def test_count_termination():
    rule = make_rule(count=3)
    assert len(expand(rule)) == 3


def test_until_is_inclusive_and_local():
    rule = make_rule(count=None, until=datetime(2026, 1, 3, 9, 0))
    assert len(expand(rule)) == 3
    rule = make_rule(count=None, until=datetime(2026, 1, 3, 8, 59))
    assert len(expand(rule)) == 2


# ------------------------------------------------------- window semantics


def test_end_is_utc_start_plus_duration_even_across_spring_forward():
    rule = make_rule(start_local=datetime(2026, 3, 8, 1, 30), duration_minutes=60, count=1)
    (w,) = expand(rule, tz=NY)
    assert w.utc_start == datetime(2026, 3, 8, 6, 30, tzinfo=UTC)
    assert w.utc_end == datetime(2026, 3, 8, 7, 30, tzinfo=UTC)  # wall end would be 03:30


def test_range_filters_on_utc_start_only():
    rule = make_rule(start_local=datetime(2026, 3, 10, 23, 30), duration_minutes=60, count=1)
    rng = (datetime(2026, 3, 10, tzinfo=UTC), datetime(2026, 3, 11, tzinfo=UTC))
    (w,) = expand(rule, rng=rng)
    assert w.utc_end == datetime(2026, 3, 11, 0, 30, tzinfo=UTC)  # end may exceed range


def test_range_is_half_open():
    rule = make_rule(start_local=datetime(2026, 3, 10, 23, 30), count=1)
    start_at = datetime(2026, 3, 10, 23, 30, tzinfo=UTC)
    # start == rangeStart -> included
    assert len(expand(rule, rng=(start_at, datetime(2026, 3, 12, tzinfo=UTC)))) == 1
    # start == rangeEnd -> excluded
    assert len(expand(rule, rng=(datetime(2026, 3, 10, tzinfo=UTC), start_at))) == 0


def test_zone_without_dst():
    rule = make_rule(start_local=datetime(2026, 3, 8, 9, 0), count=2)
    windows = expand(rule, tz=TOKYO)
    assert [w.utc_offset for w in windows] == [timedelta(hours=9)] * 2
    assert all(w.resolution == "normal" for w in windows)


def test_berlin_spring_forward_offsets():
    # Europe/Berlin 2026-03-29: CET (+01:00) -> CEST (+02:00).
    rule = make_rule(
        start_local=datetime(2026, 3, 23, 9, 0),
        frequency="WEEKLY",
        weekdays=("MO", "WE"),
        count=4,
    )
    windows = expand(rule, tz=BERLIN)
    assert [w.utc_offset for w in windows] == [
        timedelta(hours=1),
        timedelta(hours=1),
        timedelta(hours=2),
        timedelta(hours=2),
    ]


# ------------------------------------------------------- limits & sorting


def test_expansion_limit_is_shared_across_rules():
    a = make_rule(id="a", count=6000)
    b = make_rule(id="b", count=6000)
    with pytest.raises(ExpansionError) as excinfo:
        expand_rules([a, b], ZoneInfo("UTC"), "earlier", *RANGE)
    assert excinfo.value.code == "EXPANSION_LIMIT_EXCEEDED"
    assert excinfo.value.rule_id == "b"


def test_expansion_limit_boundary_exactly_at_cap_is_allowed():
    rule = make_rule(count=MAX_TOTAL_OCCURRENCES)
    wide = (datetime(2020, 1, 1, tzinfo=UTC), datetime(2060, 1, 1, tzinfo=UTC))
    assert len(expand(rule, rng=wide)) == MAX_TOTAL_OCCURRENCES


def test_sorting_by_utc_start_then_rule_id():
    early = make_rule(id="z-last", start_local=datetime(2026, 3, 10, 8, 0), count=1)
    late = make_rule(id="a-first", start_local=datetime(2026, 3, 10, 9, 0), count=1)
    same = make_rule(id="b-same", start_local=datetime(2026, 3, 10, 9, 0), count=1)
    windows = expand_rules([late, early, same], ZoneInfo("UTC"), "earlier", *RANGE)
    assert [w.rule_id for w in windows] == ["z-last", "a-first", "b-same"]
