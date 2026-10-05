"""Unit tests for the recurrence expansion core.

Covers, in particular, the DST boundaries where naive implementations
either skip a shutdown or emit the same shutdown twice:

* America/New_York spring forward (2024-03-10 02:00 -> 03:00): the
  nonexistent 02:30 must shift forward by exactly 1h.
* America/New_York fall back (2024-11-03 02:00 -> 01:00): the ambiguous
  01:30 must resolve deterministically to the earlier or later instant.
* Southern hemisphere + 30-minute transition (Australia/Lord_Howe).
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.expand import (
    AMBIGUOUS_EARLIER,
    AMBIGUOUS_LATER,
    DAILY,
    EARLIER,
    GAP_FORWARD_SHIFTED,
    LATER,
    MAX_EXPANSIONS,
    UNAMBIGUOUS,
    WEEKLY,
    ExpansionLimitExceeded,
    Rule,
    expand,
    expand_rule,
    resolve_local,
    series_size,
    _iter_local_occurrences,
)

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
LORD_HOWE = ZoneInfo("Australia/Lord_Howe")
UTC_TZ = ZoneInfo("UTC")


def dt_utc(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC) if "+" not in s and s.endswith("Z") is False else datetime.fromisoformat(
        s.replace("Z", "+00:00")
    )


def make_rule(**kw):
    defaults = dict(
        id=1,
        start_local=datetime(2024, 1, 1, 0, 0),
        duration_minutes=60,
        frequency=DAILY,
        interval=1,
        count=1,
        until_local=None,
        by_weekday=(),
    )
    defaults.update(kw)
    return Rule(**defaults)


def run(rule, tz=NY, ambiguous=EARLIER, start="2000-01-01T00:00:00Z", end="2100-01-01T00:00:00Z"):
    return expand_rule(
        rule, tz, ambiguous, dt_utc(start), dt_utc(end), [0]
    )


# ---------------------------------------------------------------- resolution

def test_unambiguous_resolution():
    utc_start, offset, res = resolve_local(datetime(2024, 6, 1, 12, 0), NY, EARLIER)
    assert utc_start == dt_utc("2024-06-01T16:00:00Z")
    assert offset == timedelta(hours=-4)
    assert res == UNAMBIGUOUS


def test_spring_forward_gap_shifts_forward_exactly_one_hour():
    # 02:30 does not exist on 2024-03-10 in New York; clocks jump 02:00 -> 03:00.
    utc_start, offset, res = resolve_local(datetime(2024, 3, 10, 2, 30), NY, EARLIER)
    assert utc_start == dt_utc("2024-03-10T07:30:00Z")
    # Effective wall clock at that instant is 03:30 EDT (-04:00).
    assert offset == timedelta(hours=-4)
    assert res == GAP_FORWARD_SHIFTED
    wall = utc_start.astimezone(NY)
    assert wall.replace(tzinfo=None) == datetime(2024, 3, 10, 3, 30)


def test_gap_at_exact_transition_start_shifts_by_jump_length():
    # 02:00 itself is the first nonexistent instant.
    utc_start, _, res = resolve_local(datetime(2024, 3, 10, 2, 0), NY, EARLIER)
    assert utc_start == dt_utc("2024-03-10T07:00:00Z")
    assert utc_start.astimezone(NY).replace(tzinfo=None) == datetime(2024, 3, 10, 3, 0)
    assert res == GAP_FORWARD_SHIFTED


def test_fall_back_ambiguous_earlier():
    # 01:30 happens twice on 2024-11-03: EDT(-4) then EST(-5).
    utc_start, offset, res = resolve_local(datetime(2024, 11, 3, 1, 30), NY, EARLIER)
    assert utc_start == dt_utc("2024-11-03T05:30:00Z")
    assert offset == timedelta(hours=-4)
    assert res == AMBIGUOUS_EARLIER


def test_fall_back_ambiguous_later():
    utc_start, offset, res = resolve_local(datetime(2024, 11, 3, 1, 30), NY, LATER)
    assert utc_start == dt_utc("2024-11-03T06:30:00Z")
    assert offset == timedelta(hours=-5)
    assert res == AMBIGUOUS_LATER


def test_ambiguous_instants_distinct_no_duplicate_shutdown():
    earlier = resolve_local(datetime(2024, 11, 3, 1, 30), NY, EARLIER)[0]
    later = resolve_local(datetime(2024, 11, 3, 1, 30), NY, LATER)[0]
    assert later - earlier == timedelta(hours=1)


def test_lord_howe_thirty_minute_spring_forward():
    # Lord Howe jumps +00:30 (10:30 -> 11:00) on 2024-10-06 02:00 wall.
    utc_start, offset, res = resolve_local(datetime(2024, 10, 6, 2, 15), LORD_HOWE, EARLIER)
    # Shift forward by exactly 30 minutes: wall becomes 02:45 at +11:00.
    assert utc_start.astimezone(LORD_HOWE).replace(tzinfo=None) == datetime(2024, 10, 6, 2, 45)
    assert offset == timedelta(hours=11)
    assert res == GAP_FORWARD_SHIFTED


def test_lord_howe_fall_back_thirty_minute_ambiguity():
    # On 2024-04-07 clocks go 02:00 (+11) -> 01:30 (+10:30); 01:45 occurs twice.
    utc_early, off_early, res_e = resolve_local(datetime(2024, 4, 7, 1, 45), LORD_HOWE, EARLIER)
    utc_late, off_late, res_l = resolve_local(datetime(2024, 4, 7, 1, 45), LORD_HOWE, LATER)
    assert off_early == timedelta(hours=11)
    assert off_late == timedelta(hours=10, minutes=30)
    assert utc_late - utc_early == timedelta(minutes=30)
    assert res_e == AMBIGUOUS_EARLIER and res_l == AMBIGUOUS_LATER


# ----------------------------------------------------------------- recurrence

def test_daily_count_basic():
    rule = make_rule(start_local=datetime(2024, 3, 8, 9, 0), count=3)
    wins = run(rule)
    assert [w.start_local for w in wins] == [
        datetime(2024, 3, 8, 9), datetime(2024, 3, 9, 9), datetime(2024, 3, 10, 9),
    ]
    # All offsets/end computed from resolved UTC start.
    assert all(w.end_utc == w.start_utc + timedelta(minutes=60) for w in wins)


def test_daily_does_not_skip_during_spring_forward():
    # A daily 02:30 maintenance must still produce one shutdown on 2024-03-10.
    rule = make_rule(start_local=datetime(2024, 3, 8, 2, 30), count=5)
    wins = run(rule)
    assert len(wins) == 5  # no skipped day
    gap_day = [w for w in wins if w.start_local.date().isoformat() == "2024-03-10"][0]
    assert gap_day.resolution == GAP_FORWARD_SHIFTED
    assert gap_day.start_utc == dt_utc("2024-03-10T07:30:00Z")
    # UTC instants are strictly ascending and spaced ~24h apart.
    for a, b in zip(wins, wins[1:]):
        assert b.start_utc > a.start_utc
        assert timedelta(hours=23) <= b.start_utc - a.start_utc <= timedelta(hours=25)


def test_daily_fall_back_emits_two_distinct_instants_not_one_duplicate():
    rule = make_rule(start_local=datetime(2024, 11, 1, 1, 30), count=5)
    wins = run(rule, ambiguous=LATER)
    assert len(wins) == 5
    utc_starts = [w.start_utc for w in wins]
    assert len(set(utc_starts)) == 5  # no duplicated shutdown instant
    fb = [w for w in wins if w.start_local.date().isoformat() == "2024-11-03"][0]
    assert fb.start_utc == dt_utc("2024-11-03T06:30:00Z")
    assert fb.resolution == AMBIGUOUS_LATER
    # The day of the fall-back the gap between UTC starts is 25h, not 24h.
    idx = wins.index(fb)
    assert wins[idx].start_utc - wins[idx - 1].start_utc == timedelta(hours=25)


def test_daily_interval_advances_on_local_calendar():
    rule = make_rule(start_local=datetime(2024, 3, 8, 2, 30), interval=2, count=3)
    wins = run(rule)
    assert [w.start_local.date().isoformat() for w in wins] == [
        "2024-03-08", "2024-03-10", "2024-03-12",
    ]


def test_daily_until_inclusive_on_local_calendar():
    rule = make_rule(
        start_local=datetime(2024, 3, 10, 2, 30),
        count=None,
        until_local=datetime(2024, 3, 14, 2, 30),
    )
    wins = run(rule)
    assert [w.start_local.date().isoformat() for w in wins] == [
        "2024-03-10", "2024-03-11", "2024-03-12", "2024-03-13", "2024-03-14",
    ]
    # The until boundary instance is a gap time; it is still included and shifted.
    assert wins[0].resolution == GAP_FORWARD_SHIFTED


def test_weekly_emits_each_selected_weekday_in_local_order():
    # 2024-03-04 is Monday; set Mon, Wed, Fri.
    rule = make_rule(
        start_local=datetime(2024, 3, 4, 3, 0),
        frequency=WEEKLY,
        interval=1,
        count=3,
        by_weekday=(1, 3, 5),
    )
    wins = run(rule)
    assert [w.start_local for w in wins] == [
        datetime(2024, 3, 4, 3), datetime(2024, 3, 6, 3), datetime(2024, 3, 8, 3),
    ]


def test_weekly_interval_strides_by_n_weeks():
    rule = make_rule(
        start_local=datetime(2024, 3, 4, 3, 0),  # Monday
        frequency=WEEKLY,
        interval=2,
        count=3,
        by_weekday=(1, 5),
    )
    wins = run(rule)
    assert [w.start_local.date().isoformat() for w in wins] == [
        "2024-03-04", "2024-03-08", "2024-03-18", "2024-03-22", "2024-04-01",
    ][:3]


def test_weekly_count_counts_instances_not_weeks():
    rule = make_rule(
        start_local=datetime(2024, 3, 4, 3, 0),
        frequency=WEEKLY,
        interval=1,
        count=4,
        by_weekday=(1, 7),
    )
    wins = run(rule)
    assert [w.start_local.date().isoformat() for w in wins] == [
        "2024-03-04", "2024-03-10", "2024-03-11", "2024-03-17",
    ]


def test_weekly_dst_no_skip_no_duplicate():
    # Daily-ish coverage across both transitions using all seven weekdays.
    rule = make_rule(
        start_local=datetime(2024, 3, 4, 2, 30),  # Monday
        frequency=WEEKLY,
        interval=1,
        count=None,
        until_local=datetime(2024, 11, 10, 2, 30),
        by_weekday=(1, 2, 3, 4, 5, 6, 7),
    )
    wins = run(rule, ambiguous=EARLIER)
    # One local occurrence per calendar day, two distinct UTC sets.
    local_days = {w.start_local.date() for w in wins}
    from datetime import date
    span = (date(2024, 11, 10) - date(2024, 3, 4)).days + 1
    assert len(local_days) == span
    assert len({w.start_utc for w in wins}) == len(wins)
    # Ascending UTC order.
    assert [w.start_utc for w in wins] == sorted(w.start_utc for w in wins)


# -------------------------------------------------------------------- filtering

def test_range_filters_only_on_start_half_open():
    rule = make_rule(start_local=datetime(2024, 3, 8, 9, 0), duration_minutes=120, count=5)
    wins = run(rule, start="2024-03-09T14:00:00Z", end="2024-03-11T13:00:00Z")
    # Mar 9 09:00 EST = 14:00Z: start == range start -> included (range is half-open
    # only at the end). Mar 11 09:00 EDT = 13:00Z: start == range end -> excluded.
    assert [w.start_local.date().isoformat() for w in wins] == ["2024-03-09", "2024-03-10"]


def test_window_crossing_range_end_still_excluded_by_start():
    rule = make_rule(start_local=datetime(2024, 3, 10, 1, 30), duration_minutes=120, count=1)
    # Window 01:30-03:30 EST spans the 07Z boundary; start 06:30Z is excluded.
    wins = run(rule, start="2024-03-10T07:00:00Z", end="2024-03-11T00:00:00Z")
    assert wins == []


# ------------------------------------------------------------------------ limits

def test_expansion_over_limit_rejected_with_rule_id():
    rule = make_rule(start_local=datetime(2000, 1, 1), count=MAX_EXPANSIONS + 1)
    counter = [0]
    with pytest.raises(ExpansionLimitExceeded) as exc:
        expand_rule(rule, UTC_TZ, EARLIER, dt_utc("1900-01-01T00:00:00Z"),
                    dt_utc("2200-01-01T00:00:00Z"), counter)
    assert exc.value.rule_id == 1


def test_exactly_limit_allowed():
    rule = make_rule(start_local=datetime(2000, 1, 1), count=MAX_EXPANSIONS)
    counter = [0]
    wins = expand_rule(rule, UTC_TZ, EARLIER, dt_utc("1900-01-01T00:00:00Z"),
                       dt_utc("2200-01-01T00:00:00Z"), counter)
    assert len(wins) == MAX_EXPANSIONS
    assert counter[0] == MAX_EXPANSIONS


def test_until_guarded_even_when_range_is_narrow():
    # A huge until span must still trip the cap even if the query window is
    # tiny; otherwise a request could be made to burn unbounded CPU.
    rule = make_rule(
        start_local=datetime(1990, 1, 1, 0, 0),
        count=None,
        until_local=datetime(2030, 1, 1, 0, 0),
    )
    with pytest.raises(ExpansionLimitExceeded) as exc:
        expand([rule], UTC_TZ, EARLIER,
               dt_utc("2024-01-01T00:00:00Z"), dt_utc("2024-01-02T00:00:00Z"))
    assert exc.value.rule_id == 1


# ------------------------------------------------------------ series_size math

def _brute_size(rule):
    out = []
    for naive in _iter_local_occurrences(rule):
        if rule.count is not None and len(out) >= rule.count:
            break
        if rule.until_local is not None and naive > rule.until_local:
            break
        out.append(naive)
    return len(out)


def test_series_size_matches_brute_force_daily():
    import itertools
    for interval in range(1, 8):
        rule = make_rule(
            start_local=datetime(2024, 3, 8, 2, 30),
            count=None,
            interval=interval,
            until_local=datetime(2025, 7, 20, 2, 30),
        )
        assert series_size(rule) == _brute_size(rule)


def test_series_size_daily_until_before_first_wall_time():
    # until is the same day but earlier wall time -> zero instances
    rule = make_rule(
        start_local=datetime(2024, 3, 10, 9, 0),
        count=None,
        until_local=datetime(2024, 3, 10, 8, 0),
    )
    assert series_size(rule) == 0


def test_series_size_matches_brute_force_weekly():
    import itertools
    base_days = (1, 3, 5, 7)
    for interval in (1, 2, 3, 7):
        for subset in [(1,), (2, 7), (3,), (1, 2, 3, 4, 5, 6, 7), (6, 7)]:
            # Anchor startLocal on the earliest selected weekday.
            rule = make_rule(
                start_local=datetime(2024, 3, 4, 3, 0)  # Monday
                if 1 in subset else
                datetime(2024, 3, 5, 3, 0),  # Tuesday
                frequency=WEEKLY,
                interval=interval,
                count=None,
                until_local=datetime(2024, 11, 17, 3, 0),
                by_weekday=subset,
            )
            if rule.start_local.isoweekday() not in subset:
                rule = make_rule(
                    start_local=datetime(2024, 3, 4, 3, 0),
                    frequency=WEEKLY, interval=interval, count=None,
                    until_local=datetime(2024, 11, 17, 3, 0),
                    by_weekday=subset + (1,),
                )
            assert series_size(rule) == _brute_size(rule), subset


def test_series_size_count_rule():
    assert series_size(make_rule(count=10001)) == 10001


def test_sorting_by_utc_then_rule_id_is_stable():
    r1 = make_rule(id=1, start_local=datetime(2024, 6, 1, 12, 0), count=1)
    r2 = make_rule(id=2, start_local=datetime(2024, 6, 1, 12, 0), count=1)   # same UTC 12:00Z
    r3 = make_rule(id=3, start_local=datetime(2024, 6, 1, 11, 0), count=1)   # 11:00Z
    wins = expand([r1, r2, r3], UTC_TZ, EARLIER,
                  dt_utc("2000-01-01T00:00:00Z"), dt_utc("2100-01-01T00:00:00Z"))
    assert [(w.start_utc, w.rule_id) for w in wins] == sorted(
        (w.start_utc, w.rule_id) for w in wins
    )
    # Tie between rule 1 and 2 at 12:00Z resolved by id.
    ties = [w for w in wins if w.start_utc == dt_utc("2024-06-01T12:00:00Z")]
    assert [w.rule_id for w in ties] == [1, 2]
