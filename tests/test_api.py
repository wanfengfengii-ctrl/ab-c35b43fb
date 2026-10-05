"""HTTP-level tests for POST /api/maintenance-windows/expand.

These double as the DST-boundary smoke tests: they exercise the full ASGI
stack including validation, error codes and JSON encoding.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PATH = "/api/maintenance-windows/expand"


def daily_rule(rid=1, **kw):
    rule = {
        "id": rid,
        "startLocal": "2024-03-08T02:30:00",
        "durationMinutes": 60,
        "frequency": "DAILY",
        "interval": 1,
        "count": 5,
    }
    rule.update(kw)
    return rule


def base_payload(**kw):
    payload = {
        "timeZone": "America/New_York",
        "rangeStartUtc": "2024-01-01T00:00:00Z",
        "rangeEndUtc": "2025-01-01T00:00:00Z",
        "ambiguousTime": "earlier",
        "rules": [daily_rule()],
    }
    payload.update(kw)
    return payload


def post(payload):
    return client.post(PATH, json=payload)


# ----------------------------------------------------------------- happy paths

def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_daily_dst_spring_forward_shifted_and_not_skipped():
    r = post(base_payload())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 5
    gap = next(w for w in body["windows"] if w["startLocal"].startswith("2024-03-10"))
    assert gap["startUtc"] == "2024-03-10T07:30:00Z"   # 02:30 -> shifted +1h
    assert gap["endUtc"] == "2024-03-10T08:30:00Z"
    assert gap["utcOffset"] == "-04:00"
    assert gap["resolution"] == "GAP_FORWARD_SHIFTED"
    plain = next(w for w in body["windows"] if w["startLocal"].startswith("2024-03-09"))
    assert plain["utcOffset"] == "-05:00"
    assert plain["resolution"] == "UNAMBIGUOUS"


def test_dst_fall_back_earlier_and_later_pick_distinct_instants():
    rule = daily_rule(startLocal="2024-11-01T01:30:00", count=5)
    earlier = post(base_payload(ambiguousTime="earlier", rules=[rule])).json()
    later = post(base_payload(ambiguousTime="later", rules=[rule])).json()
    e = next(w for w in earlier["windows"] if w["startLocal"].startswith("2024-11-03"))
    l = next(w for w in later["windows"] if w["startLocal"].startswith("2024-11-03"))
    assert (e["startUtc"], e["utcOffset"], e["resolution"]) == (
        "2024-11-03T05:30:00Z", "-04:00", "AMBIGUOUS_EARLIER")
    assert (l["startUtc"], l["utcOffset"], l["resolution"]) == (
        "2024-11-03T06:30:00Z", "-05:00", "AMBIGUOUS_LATER")
    # Every occurrence stays a distinct, ascending UTC instant: no skipped or
    # duplicated shutdown across the transition.
    starts = [w["startUtc"] for w in later["windows"]]
    assert starts == sorted(starts) and len(set(starts)) == 5


def test_weekly_multi_rule_stable_sorting_and_offset_returned():
    rules = [
        daily_rule(rid=2, startLocal="2024-03-09T08:00:00", count=1),
        {
            "id": 1,
            "startLocal": "2024-03-08T03:00:00",  # Friday
            "durationMinutes": 30,
            "frequency": "WEEKLY",
            "interval": 1,
            "untilLocal": "2024-03-15T03:00:00",
            "byWeekday": [1, 5],  # Mon, Fri
        },
    ]
    r = post(base_payload(rules=rules))
    assert r.status_code == 200, r.text
    wins = r.json()["windows"]
    keys = [(w["startUtc"], w["ruleId"]) for w in wins]
    assert keys == sorted(keys)
    # Original local times preserved alongside adopted offsets.
    for w in wins:
        assert "startLocal" in w and w["utcOffset"] in ("-05:00", "-04:00")


def test_half_open_range_filters_on_start_only():
    r = post(base_payload(
        rangeStartUtc="2024-03-09T14:00:00Z",   # == Mar 9 09:00 local, unrelated
        rangeEndUtc="2024-03-12T06:30:00Z",     # == Mar 12 02:30 EDT -> excluded
    ))
    days = [w["startLocal"][:10] for w in r.json()["windows"]]
    assert "2024-03-12" not in days


def test_iso_offset_accepted_for_range():
    r = post(base_payload(
        rangeStartUtc="2024-01-01T01:00:00+01:00",
        rangeEndUtc="2025-01-01T00:00:00+00:00",
    ))
    assert r.status_code == 200, r.text


# --------------------------------------------------------------- rejection path

def assert_rejected(payload, code=None, rule_id=None, status=422):
    r = post(payload)
    assert r.status_code == status, r.text
    err = r.json()["error"]
    if code is not None:
        assert err["code"] == code
    if rule_id is not None:
        assert err.get("ruleId") == rule_id
    return err


def test_unknown_timezone_rejected():
    assert_rejected(base_payload(timeZone="Mars/Olympus_Mons"), "UNKNOWN_TIMEZONE")


def test_unbounded_rule_rejected_with_rule_id():
    rule = daily_rule()
    del rule["count"]
    err = assert_rejected(base_payload(rules=[rule]), "UNBOUNDED_RULE", rule_id=1)
    assert "rule 1" in err["message"]


def test_both_count_and_until_contradictory():
    assert_rejected(
        base_payload(rules=[daily_rule(untilLocal="2024-12-31T00:00:00")]),
        "CONTRADICTORY_TERMINATION", rule_id=1)


def test_until_before_start_contradictory():
    assert_rejected(
        base_payload(rules=[daily_rule(
            count=None, untilLocal="2024-03-01T00:00:00")]),
        "CONTRADICTORY_TERMINATION", rule_id=1)


def test_invalid_local_datetime_string_rejected():
    assert_rejected(
        base_payload(rules=[daily_rule(startLocal="2024-03-08T25:00:00")]),
        rule_id=1)


def test_local_time_with_offset_suffix_rejected():
    assert_rejected(
        base_payload(rules=[daily_rule(startLocal="2024-03-08T02:30:00-05:00")]),
        rule_id=1)


def test_range_without_timezone_rejected():
    assert_rejected(base_payload(rangeStartUtc="2024-01-01T00:00:00"))


def test_invalid_range_order_rejected():
    assert_rejected(
        base_payload(
            rangeStartUtc="2024-06-01T00:00:00Z",
            rangeEndUtc="2024-01-01T00:00:00Z"),
        "INVALID_RANGE")


def test_bad_ambiguity_policy_rejected():
    assert_rejected(base_payload(ambiguousTime="maybe"), "INVALID_AMBIGUITY_POLICY")


def test_duplicate_rule_ids_rejected():
    assert_rejected(
        base_payload(rules=[daily_rule(1), daily_rule(1, startLocal="2024-04-01T00:00:00")]),
        "DUPLICATE_RULE_ID", rule_id=1)


def test_weekly_requires_distinct_weekday_set():
    w = {
        "id": 1, "startLocal": "2024-03-04T03:00:00", "durationMinutes": 10,
        "frequency": "WEEKLY", "interval": 1, "count": 3, "byWeekday": [1, 1],
    }
    assert_rejected(base_payload(rules=[w]), "DUPLICATE_WEEKDAY", rule_id=1)


def test_weekly_requires_weekday_set():
    w = {
        "id": 7, "startLocal": "2024-03-04T03:00:00", "durationMinutes": 10,
        "frequency": "WEEKLY", "interval": 1, "count": 3,
    }
    assert_rejected(base_payload(rules=[w]), "MISSING_WEEKDAYS", rule_id=7)


def test_weekly_weekday_out_of_range():
    w = {
        "id": 1, "startLocal": "2024-03-04T03:00:00", "durationMinutes": 10,
        "frequency": "WEEKLY", "interval": 1, "count": 3, "byWeekday": [8],
    }
    assert_rejected(base_payload(rules=[w]), "INVALID_WEEKDAY", rule_id=1)


def test_weekly_start_weekday_must_be_in_set():
    # 2024-03-05 is Tuesday; set only contains Monday.
    w = {
        "id": 3, "startLocal": "2024-03-05T03:00:00", "durationMinutes": 10,
        "frequency": "WEEKLY", "interval": 1, "count": 3, "byWeekday": [1],
    }
    assert_rejected(base_payload(rules=[w]), "START_WEEKDAY_NOT_IN_SET", rule_id=3)


def test_daily_with_byweekday_rejected():
    assert_rejected(
        base_payload(rules=[daily_rule(byWeekday=[1])]),
        "UNEXPECTED_WEEKDAYS", rule_id=1)


def test_bad_frequency_schema_error_keeps_rule_id():
    bad = daily_rule()
    bad["frequency"] = "MONTHLY"
    assert_rejected(base_payload(rules=[bad]), rule_id=1)


def test_zero_duration_rejected():
    assert_rejected(base_payload(rules=[daily_rule(durationMinutes=0)]), rule_id=1)


def test_interval_out_of_bounds_rejected():
    assert_rejected(base_payload(rules=[daily_rule(interval=31)]), rule_id=1)


def test_too_many_rules_rejected():
    rules = [daily_rule(i, startLocal="2024-01-01T00:00:00", count=1) for i in range(1, 22)]
    r = post(base_payload(rules=rules))
    assert r.status_code in (400, 422)


def test_empty_rules_rejected():
    r = post(base_payload(rules=[]))
    assert r.status_code in (400, 422)


def test_expansion_over_ten_thousand_rejected():
    # 10001 daily instances between 2000 and 2030.
    rule = daily_rule(
        startLocal="2000-01-01T00:00:00",
        count=None,
        untilLocal="2030-01-01T00:00:00",
    )
    r = post(base_payload(
        timeZone="UTC",
        rangeStartUtc="2024-01-01T00:00:00Z",
        rangeEndUtc="2024-01-02T00:00:00Z",
        rules=[rule]))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "EXPANSION_LIMIT_EXCEEDED" and err["ruleId"] == 1


def test_exactly_ten_thousand_allowed():
    rule = daily_rule(
        startLocal="2000-01-01T00:00:00",
        count=10000,
    )
    r = post(base_payload(
        timeZone="UTC",
        rangeStartUtc="1999-01-01T00:00:00Z",
        rangeEndUtc="2030-01-01T00:00:00Z",
        rules=[rule]))
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 10000


def test_second_rule_failure_is_localized():
    good = daily_rule(1)
    bad = daily_rule(2, count=None)  # unbounded
    assert_rejected(base_payload(rules=[good, bad]), "UNBOUNDED_RULE", rule_id=2)


def test_expansion_cap_counts_across_all_rules():
    rules = [
        daily_rule(1, startLocal="2000-01-01T00:00:00", count=6000),
        daily_rule(2, startLocal="2000-01-01T00:00:00", count=4001),
    ]
    r = post(base_payload(
        timeZone="UTC",
        rangeStartUtc="2024-01-01T00:00:00Z",
        rangeEndUtc="2024-01-02T00:00:00Z",
        rules=rules))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "EXPANSION_LIMIT_EXCEEDED" and err["ruleId"] == 2
