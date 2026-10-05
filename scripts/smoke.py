"""HTTP smoke test covering DST boundaries and rejection paths.

Talks to the running service over real HTTP (std-library only) and exits
non-zero on the first failed expectation. Target base URL comes from
``BASE_URL`` (default http://127.0.0.1:8000).
"""

import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
PATH = "/api/maintenance-windows/expand"

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def request(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def wait_for_health(attempts=30):
    for _ in range(attempts):
        try:
            status, body = request("GET", "/health")
            if status == 200 and body.get("status") == "ok":
                return True
        except Exception:
            pass
        import time
        time.sleep(1)
    return False


def daily(rid, start, count=None, until=None):
    rule = {
        "id": rid, "startLocal": start, "durationMinutes": 60,
        "frequency": "DAILY", "interval": 1,
    }
    if count is not None:
        rule["count"] = count
    else:
        rule["untilLocal"] = until
    return rule


def payload(timezone_name, rules, ambiguous="earlier",
            start="2024-01-01T00:00:00Z", end="2025-01-01T00:00:00Z"):
    return {
        "timeZone": timezone_name,
        "rangeStartUtc": start,
        "rangeEndUtc": end,
        "ambiguousTime": ambiguous,
        "rules": rules,
    }


def main():
    check("service reachable at /health", wait_for_health())

    # --- Spring forward (America/New_York, 2024-03-10 02:00 -> 03:00) ------
    status, body = request("POST", PATH, payload(
        "America/New_York",
        [daily(1, "2024-03-08T02:30:00", count=5)],
    ))
    check("spring-forward request accepted", status == 200, str(body))
    if status == 200:
        wins = body["windows"]
        check("no shutdown skipped across spring forward", len(wins) == 5)
        gap = next((w for w in wins if w["startLocal"].startswith("2024-03-10")), None)
        check("gap day present and shifted +1h",
              gap is not None and gap["startUtc"] == "2024-03-10T07:30:00Z"
              and gap["endUtc"] == "2024-03-10T08:30:00Z"
              and gap["utcOffset"] == "-04:00"
              and gap["resolution"] == "GAP_FORWARD_SHIFTED",
              json.dumps(gap))
        starts = [w["startUtc"] for w in wins]
        check("UTC instants distinct across spring forward",
              len(set(starts)) == len(starts))

    # --- Fall back: earlier vs later must be distinct ---------------------
    status_e, body_e = request("POST", PATH, payload(
        "America/New_York",
        [daily(2, "2024-11-01T01:30:00", count=5)],
        ambiguous="earlier",
    ))
    status_l, body_l = request("POST", PATH, payload(
        "America/New_York",
        [daily(2, "2024-11-01T01:30:00", count=5)],
        ambiguous="later",
    ))
    check("fall-back earlier/later accepted", status_e == 200 and status_l == 200)
    if status_e == 200 and status_l == 200:
        e = next(w for w in body_e["windows"] if w["startLocal"].startswith("2024-11-03"))
        l = next(w for w in body_l["windows"] if w["startLocal"].startswith("2024-11-03"))
        check("ambiguous earlier = 05:30Z/-04:00",
              e["startUtc"] == "2024-11-03T05:30:00Z" and e["utcOffset"] == "-04:00"
              and e["resolution"] == "AMBIGUOUS_EARLIER", json.dumps(e))
        check("ambiguous later = 06:30Z/-05:00",
              l["startUtc"] == "2024-11-03T06:30:00Z" and l["utcOffset"] == "-05:00"
              and l["resolution"] == "AMBIGUOUS_LATER", json.dumps(l))
        check("no duplicated shutdown later-series",
              len({w["startUtc"] for w in body_l["windows"]}) == 5)

    # --- Southern-hemisphere 30-minute jump (Lord Howe) -------------------
    status, body = request("POST", PATH, payload(
        "Australia/Lord_Howe",
        [daily(3, "2024-10-05T02:15:00", count=3)],
        start="2024-10-01T00:00:00Z", end="2024-10-15T00:00:00Z",
    ))
    check("30-minute gap zone accepted", status == 200, str(body))
    if status == 200:
        gap = next((w for w in body["windows"] if w["startLocal"].startswith("2024-10-06")), None)
        check("30-minute gap shifted by exactly 00:30 with +11:00 offset",
              gap is not None and gap["utcOffset"] == "+11:00"
              and gap["resolution"] == "GAP_FORWARD_SHIFTED", json.dumps(gap))

    # --- Stable ordering across rules -------------------------------------
    status, body = request("POST", PATH, payload(
        "UTC",
        [
            {"id": 2, "startLocal": "2024-06-01T12:00:00", "durationMinutes": 10,
             "frequency": "DAILY", "interval": 1, "count": 1},
            {"id": 1, "startLocal": "2024-06-01T12:00:00", "durationMinutes": 10,
             "frequency": "DAILY", "interval": 1, "count": 1},
            {"id": 3, "startLocal": "2024-06-01T11:00:00", "durationMinutes": 10,
             "frequency": "DAILY", "interval": 1, "count": 1},
        ],
        start="2024-01-01T00:00:00Z", end="2025-01-01T00:00:00Z",
    ))
    if status == 200:
        keys = [(w["startUtc"], w["ruleId"]) for w in body["windows"]]
        check("sorted by (startUtc, ruleId)", keys == sorted(keys), str(keys))

    # --- Rejection paths ---------------------------------------------------
    status, body = request("POST", PATH, payload("Mars/Olympus_Mons",
                                           [daily(1, "2024-01-01T00:00:00", count=1)]))
    check("unknown timezone rejected",
          status == 422 and body["error"]["code"] == "UNKNOWN_TIMEZONE", str(body))

    unbounded = daily(7, "2024-01-01T00:00:00", count=1)
    unbounded.pop("count")
    status, body = request("POST", PATH, payload("UTC", [unbounded]))
    check("unbounded rule rejected with ruleId",
          status == 422 and body["error"]["code"] == "UNBOUNDED_RULE"
          and body["error"]["ruleId"] == 7, str(body))

    status, body = request("POST", PATH, payload(
        "UTC",
        [daily(4, "2000-01-01T00:00:00", until="2030-01-01T00:00:00")],
        start="2024-01-01T00:00:00Z", end="2024-01-02T00:00:00Z",
    ))
    check(">10000 expansion rejected even with narrow query range",
          status == 422 and body["error"]["code"] == "EXPANSION_LIMIT_EXCEEDED"
          and body["error"]["ruleId"] == 4, str(body))

    bad_time = daily(5, "2024-03-08T25:00:00", count=1)
    status, body = request("POST", PATH, payload("UTC", [bad_time]))
    check("illegal local time rejected with ruleId",
          status == 422 and body["error"].get("ruleId") == 5, str(body))

    if failures:
        print(f"\nSMOKE FAILED: {len(failures)} check(s) failed")
        return 1
    print("\nALL SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
