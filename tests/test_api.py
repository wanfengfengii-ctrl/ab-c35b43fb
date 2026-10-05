"""API-level tests: validation, error reporting, and response shape."""
import copy

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

VALID_PAYLOAD = {
    "timezone": "America/New_York",
    "rangeStartUtc": "2026-03-01T00:00:00Z",
    "rangeEndUtc": "2026-04-01T00:00:00Z",
    "ambiguousTimePolicy": "earlier",
    "rules": [
        {
            "id": "daily-standup",
            "startLocal": "2026-03-08T02:30:00",
            "durationMinutes": 30,
            "frequency": "DAILY",
            "interval": 1,
            "count": 3,
        }
    ],
}


def payload(**overrides):
    data = copy.deepcopy(VALID_PAYLOAD)
    data.update(overrides)
    return data


def rule(**overrides):
    base = copy.deepcopy(VALID_PAYLOAD["rules"][0])
    base.update(overrides)
    return base


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_expand_gap_shifted_response_shape():
    resp = client.post("/api/maintenance-windows/expand", json=payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["timezone"] == "America/New_York"
    assert body["total"] == 3
    first = body["windows"][0]
    # 2026-03-08 02:30 does not exist in New York -> shifted to 03:30 (-04:00).
    assert first["ruleId"] == "daily-standup"
    assert first["localStart"] == "2026-03-08T02:30:00"
    assert first["utcStart"] == "2026-03-08T07:30:00Z"
    assert first["utcEnd"] == "2026-03-08T08:00:00Z"
    assert first["utcOffset"] == "-04:00"
    assert first["resolution"] == "gap-shifted"


def test_ambiguous_policy_changes_chosen_instant():
    r = rule(startLocal="2026-11-01T01:30:00", count=1)
    base = payload(
        rangeStartUtc="2026-11-01T00:00:00Z",
        rangeEndUtc="2026-11-02T00:00:00Z",
        rules=[r],
    )
    earlier = client.post(
        "/api/maintenance-windows/expand", json={**base, "ambiguousTimePolicy": "earlier"}
    ).json()["windows"][0]
    later = client.post(
        "/api/maintenance-windows/expand", json={**base, "ambiguousTimePolicy": "later"}
    ).json()["windows"][0]
    assert earlier["utcStart"] == "2026-11-01T05:30:00Z"
    assert earlier["utcOffset"] == "-04:00"
    assert later["utcStart"] == "2026-11-01T06:30:00Z"
    assert later["utcOffset"] == "-05:00"


def test_results_sorted_by_utc_start_then_rule_id():
    rules = [
        rule(id="b", startLocal="2026-03-10T09:00:00", count=1),
        rule(id="a", startLocal="2026-03-10T09:00:00", count=1),
        rule(id="c", startLocal="2026-03-09T09:00:00", count=1),
    ]
    body = client.post("/api/maintenance-windows/expand", json=payload(rules=rules)).json()
    assert [w["ruleId"] for w in body["windows"]] == ["c", "a", "b"]


def test_unknown_timezone_rejected():
    resp = client.post("/api/maintenance-windows/expand", json=payload(timezone="Mars/Olympus"))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "UNKNOWN_TIMEZONE"


def test_both_count_and_until_rejected_with_rule_id():
    bad = rule(id="r-both", until="2026-03-20T00:00:00")
    resp = client.post("/api/maintenance-windows/expand", json=payload(rules=[bad]))
    assert resp.status_code == 422
    text = resp.text
    assert "r-both" in text


def test_unbounded_rule_rejected():
    bad = rule(id="r-open")
    del bad["count"]
    resp = client.post("/api/maintenance-windows/expand", json=payload(rules=[bad]))
    assert resp.status_code == 422
    assert "r-open" in resp.text


def test_duplicate_rule_ids_rejected():
    rules = [rule(id="dup"), rule(id="dup", startLocal="2026-03-09T02:30:00")]
    resp = client.post("/api/maintenance-windows/expand", json=payload(rules=rules))
    assert resp.status_code == 422
    assert "dup" in resp.json()["error"]["message"]


def test_rule_count_bounds():
    assert client.post(
        "/api/maintenance-windows/expand", json=payload(rules=[])
    ).status_code == 422
    many = [rule(id=f"r{i}", startLocal=f"2026-03-{10 + i % 15:02d}T09:00:00") for i in range(21)]
    assert client.post(
        "/api/maintenance-windows/expand", json=payload(rules=many)
    ).status_code == 422


def test_interval_bounds():
    assert client.post(
        "/api/maintenance-windows/expand", json=payload(rules=[rule(interval=0)])
    ).status_code == 422
    assert client.post(
        "/api/maintenance-windows/expand", json=payload(rules=[rule(interval=31)])
    ).status_code == 422


def test_weekly_requires_distinct_weekdays():
    no_days = rule(frequency="WEEKLY")
    assert client.post(
        "/api/maintenance-windows/expand", json=payload(rules=[no_days])
    ).status_code == 422
    dup_days = rule(frequency="WEEKLY", weekdays=["MO", "MO"])
    assert client.post(
        "/api/maintenance-windows/expand", json=payload(rules=[dup_days])
    ).status_code == 422


def test_daily_rejects_weekdays():
    bad = rule(weekdays=["MO"])
    resp = client.post("/api/maintenance-windows/expand", json=payload(rules=[bad]))
    assert resp.status_code == 422


def test_aware_local_times_rejected():
    bad = rule(startLocal="2026-03-08T02:30:00Z")
    assert client.post(
        "/api/maintenance-windows/expand", json=payload(rules=[bad])
    ).status_code == 422
    bad2 = rule(count=None, until="2026-03-20T00:00:00+02:00")
    assert client.post(
        "/api/maintenance-windows/expand", json=payload(rules=[bad2])
    ).status_code == 422


def test_until_before_start_rejected():
    bad = rule(count=None, until="2026-03-01T00:00:00")
    resp = client.post("/api/maintenance-windows/expand", json=payload(rules=[bad]))
    assert resp.status_code == 422
    assert "until" in resp.text


def test_range_validation():
    assert client.post(
        "/api/maintenance-windows/expand", json=payload(rangeStartUtc="2026-03-01T00:00:00")
    ).status_code == 422
    assert client.post(
        "/api/maintenance-windows/expand",
        json=payload(rangeStartUtc="2026-04-01T00:00:00Z", rangeEndUtc="2026-03-01T00:00:00Z"),
    ).status_code == 422


def test_expansion_limit_exceeded_reports_rule():
    huge = rule(id="huge", count=20000)
    resp = client.post("/api/maintenance-windows/expand", json=payload(rules=[huge]))
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "EXPANSION_LIMIT_EXCEEDED"
    assert error["ruleId"] == "huge"


def test_unknown_fields_rejected():
    bad = payload(extraField=True)
    assert client.post("/api/maintenance-windows/expand", json=bad).status_code == 422
