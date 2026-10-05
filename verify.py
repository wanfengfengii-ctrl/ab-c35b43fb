#!/usr/bin/env python3
"""One-shot verification: unit tests -> build check -> DST-boundary HTTP smoke.

Runs inside the ``verify`` compose service against the freshly started ``app``
service. Exit code 0 means every stage passed; 1 means at least one failed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

BASE_URL = os.environ.get("APP_BASE_URL", "http://app:8080")
EXPAND_URL = BASE_URL + "/api/maintenance-windows/expand"

FAILURES: list = []


def step(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def run(cmd: list) -> int:
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd).returncode


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not condition else ""), flush=True)
    if not condition:
        FAILURES.append(name)


def http(method: str, url: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def wait_for_app(timeout_s: int = 90) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            status, body = http("GET", BASE_URL + "/health")
            if status == 200 and body.get("status") == "ok":
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


# ------------------------------------------------------------------ stages

step("1/3 code tests (pytest)")
if run([sys.executable, "-m", "pytest", "-q", "tests"]) != 0:
    FAILURES.append("unit tests")

step("2/3 build (byte-compile all sources)")
if run([sys.executable, "-m", "compileall", "-q", "app", "verify.py"]) != 0:
    FAILURES.append("build")

step("3/3 HTTP smoke against a clean app startup")
check("app becomes healthy", wait_for_app(), f"no /health 200 from {BASE_URL}")

if not FAILURES or True:  # smoke still runs to report everything at once
    # --- spring-forward gap: nonexistent wall time shifts by exact jump ----
    status, body = http("POST", EXPAND_URL, {
        "timezone": "America/New_York",
        "rangeStartUtc": "2026-03-01T00:00:00Z",
        "rangeEndUtc": "2026-04-01T00:00:00Z",
        "ambiguousTimePolicy": "earlier",
        "rules": [{
            "id": "gap", "startLocal": "2026-03-08T02:30:00",
            "durationMinutes": 30, "frequency": "DAILY", "interval": 1, "count": 1,
        }],
    })
    ok = (
        status == 200 and body.get("total") == 1
        and body["windows"][0]["localStart"] == "2026-03-08T02:30:00"
        and body["windows"][0]["utcStart"] == "2026-03-08T07:30:00Z"
        and body["windows"][0]["utcEnd"] == "2026-03-08T08:00:00Z"
        and body["windows"][0]["utcOffset"] == "-04:00"
    )
    check("spring-forward gap shifts 02:30 -> 03:30 local (07:30Z, -04:00)", ok,
          f"status={status} body={json.dumps(body)[:400]}")

    # --- fall-back fold: ambiguous wall time honours earlier/later ---------
    fold_payload = {
        "timezone": "America/New_York",
        "rangeStartUtc": "2026-11-01T00:00:00Z",
        "rangeEndUtc": "2026-11-02T00:00:00Z",
        "rules": [{
            "id": "fold", "startLocal": "2026-11-01T01:30:00",
            "durationMinutes": 45, "frequency": "DAILY", "interval": 1, "count": 1,
        }],
    }
    s1, b1 = http("POST", EXPAND_URL, {**fold_payload, "ambiguousTimePolicy": "earlier"})
    s2, b2 = http("POST", EXPAND_URL, {**fold_payload, "ambiguousTimePolicy": "later"})
    ok = (
        s1 == 200 and s2 == 200
        and b1["windows"][0]["utcStart"] == "2026-11-01T05:30:00Z"
        and b1["windows"][0]["utcOffset"] == "-04:00"
        and b2["windows"][0]["utcStart"] == "2026-11-01T06:30:00Z"
        and b2["windows"][0]["utcOffset"] == "-05:00"
    )
    check("fall-back fold: earlier=05:30Z/-04:00, later=06:30Z/-05:00", ok,
          f"earlier={json.dumps(b1)[:300]} later={json.dumps(b2)[:300]}")

    # --- weekly rule across a DST boundary keeps local wall time -----------
    status, body = http("POST", EXPAND_URL, {
        "timezone": "Europe/Berlin",
        "rangeStartUtc": "2026-03-01T00:00:00Z",
        "rangeEndUtc": "2026-05-01T00:00:00Z",
        "ambiguousTimePolicy": "earlier",
        "rules": [{
            "id": "weekly", "startLocal": "2026-03-23T09:00:00",
            "durationMinutes": 60, "frequency": "WEEKLY", "interval": 1,
            "weekdays": ["MO", "WE"], "count": 4,
        }],
    })
    starts = [w["utcStart"] for w in body.get("windows", [])]
    offsets = [w["utcOffset"] for w in body.get("windows", [])]
    ok = (
        status == 200
        and starts == [
            "2026-03-23T08:00:00Z", "2026-03-25T08:00:00Z",
            "2026-03-30T07:00:00Z", "2026-04-01T07:00:00Z",
        ]
        and offsets == ["+01:00", "+01:00", "+02:00", "+02:00"]
    )
    check("weekly MO/WE across Berlin DST keeps 09:00 local", ok,
          f"status={status} starts={starts} offsets={offsets}")

    # --- half-open range filters on UTC start only --------------------------
    status, body = http("POST", EXPAND_URL, {
        "timezone": "UTC",
        "rangeStartUtc": "2026-03-10T00:00:00Z",
        "rangeEndUtc": "2026-03-11T00:00:00Z",
        "rules": [{
            "id": "edge", "startLocal": "2026-03-10T23:30:00",
            "durationMinutes": 60, "frequency": "DAILY", "interval": 1, "count": 1,
        }],
    })
    ok = (
        status == 200 and body.get("total") == 1
        and body["windows"][0]["utcEnd"] == "2026-03-11T00:30:00Z"
    )
    check("window starting inside range is kept even when its end spills out", ok,
          f"status={status} body={json.dumps(body)[:300]}")

    # --- error handling ------------------------------------------------------
    status, body = http("POST", EXPAND_URL, {
        "timezone": "Not/AZone",
        "rangeStartUtc": "2026-03-01T00:00:00Z",
        "rangeEndUtc": "2026-04-01T00:00:00Z",
        "rules": [{
            "id": "x", "startLocal": "2026-03-08T02:30:00",
            "durationMinutes": 30, "frequency": "DAILY", "interval": 1, "count": 1,
        }],
    })
    check("unknown timezone -> 400 UNKNOWN_TIMEZONE",
          status == 400 and body.get("error", {}).get("code") == "UNKNOWN_TIMEZONE",
          f"status={status} body={json.dumps(body)[:300]}")

    status, body = http("POST", EXPAND_URL, {
        "timezone": "UTC",
        "rangeStartUtc": "2026-03-01T00:00:00Z",
        "rangeEndUtc": "2026-04-01T00:00:00Z",
        "rules": [{
            "id": "huge", "startLocal": "2026-03-01T00:00:00",
            "durationMinutes": 5, "frequency": "DAILY", "interval": 1, "count": 20000,
        }],
    })
    err = body.get("error", {})
    check("expansion over 10000 items -> 400 with rule id",
          status == 400 and err.get("code") == "EXPANSION_LIMIT_EXCEEDED"
          and err.get("ruleId") == "huge",
          f"status={status} body={json.dumps(body)[:300]}")

    status, body = http("POST", EXPAND_URL, {
        "timezone": "UTC",
        "rangeStartUtc": "2026-03-01T00:00:00Z",
        "rangeEndUtc": "2026-04-01T00:00:00Z",
        "rules": [{
            "id": "bad-term", "startLocal": "2026-03-01T00:00:00",
            "durationMinutes": 5, "frequency": "DAILY", "interval": 1,
            "count": 2, "until": "2026-03-10T00:00:00",
        }],
    })
    check("contradictory termination (count+until) -> 422 naming the rule",
          status == 422 and "bad-term" in json.dumps(body),
          f"status={status} body={json.dumps(body)[:300]}")

# ------------------------------------------------------------------ verdict
print(flush=True)
if FAILURES:
    print(f"VERIFY FAILED ({len(FAILURES)}): {', '.join(FAILURES)}", flush=True)
    sys.exit(1)
print("VERIFY OK: tests, build and DST smoke all passed", flush=True)
sys.exit(0)
