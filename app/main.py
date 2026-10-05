"""HTTP layer for maintenance-window expansion.

``POST /api/maintenance-windows/expand``
    Validates the request, expands every rule in the given IANA timezone
    and returns UTC intervals sorted by ``(startUtc, ruleId)``.

All semantic failures use HTTP 422 with a body of::

    {"error": {"code": "...", "message": "...", "ruleId": 2}}

so a client can locate the offending rule. ``ruleId`` is omitted for
request-level failures (unknown timezone, bad range, ...).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .expand import (
    DAILY,
    EARLIER,
    LATER,
    WEEKLY,
    ExpansionLimitExceeded,
    Rule,
    expand,
)

UTC = timezone.utc


class RuleModel(BaseModel):
    id: int = Field(ge=1)
    startLocal: str
    durationMinutes: int = Field(ge=1)
    frequency: str
    interval: int = Field(ge=1, le=30)
    count: Optional[int] = Field(default=None, ge=1)
    untilLocal: Optional[str] = None
    byWeekday: Optional[list[int]] = None


class ExpandRequest(BaseModel):
    timeZone: str
    rangeStartUtc: str
    rangeEndUtc: str
    ambiguousTime: str
    rules: list[RuleModel] = Field(min_length=1, max_length=20)


def error(code: str, message: str, status: int = 422, rule_id=None) -> JSONResponse:
    body: dict = {"code": code, "message": message}
    if rule_id is not None:
        body["ruleId"] = rule_id
    return JSONResponse(status_code=status, content={"error": body})


def parse_naive(value: str, field: str, rule_id: int) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise ReqError(f"rule {rule_id}: {field} is not a valid ISO-8601 datetime", rule_id)
    if dt.tzinfo is not None:
        raise ReqError(
            f"rule {rule_id}: {field} must be a local datetime without timezone suffix",
            rule_id,
        )
    return dt


def parse_utc_instant(value: str, field: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise ReqError(f"{field} is not a valid ISO-8601 datetime")
    if dt.tzinfo is None:
        raise ReqError(f"{field} must carry a timezone (use 'Z' or an offset)")
    return dt.astimezone(UTC)


class ReqError(Exception):
    def __init__(self, message: str, rule_id=None, code: str = "VALIDATION_ERROR"):
        super().__init__(message)
        self.message = message
        self.rule_id = rule_id
        self.code = code


def validate_and_build(req: ExpandRequest) -> tuple[ZoneInfo, str, datetime, datetime, list[Rule]]:
    if req.ambiguousTime not in (EARLIER, LATER):
        raise ReqError(
            f"ambiguousTime must be 'earlier' or 'later', got {req.ambiguousTime!r}",
            code="INVALID_AMBIGUITY_POLICY",
        )

    try:
        tz = ZoneInfo(req.timeZone)
    except ZoneInfoNotFoundError:
        raise ReqError(
            f"unknown IANA timezone: {req.timeZone!r}", code="UNKNOWN_TIMEZONE"
        )

    range_start = parse_utc_instant(req.rangeStartUtc, "rangeStartUtc")
    range_end = parse_utc_instant(req.rangeEndUtc, "rangeEndUtc")
    if not range_start < range_end:
        raise ReqError("rangeStartUtc must be strictly before rangeEndUtc",
                       code="INVALID_RANGE")

    seen_ids: set[int] = set()
    rules: list[Rule] = []
    for r in req.rules:
        rid = r.id
        if rid in seen_ids:
            raise ReqError(f"rule {rid}: duplicate rule id", rid, "DUPLICATE_RULE_ID")
        seen_ids.add(rid)

        if r.frequency not in (DAILY, WEEKLY):
            raise ReqError(
                f"rule {rid}: frequency must be DAILY or WEEKLY, got {r.frequency!r}",
                rid,
            )

        start_local = parse_naive(r.startLocal, "startLocal", rid)

        if (r.count is None) == (r.untilLocal is None):
            if r.count is not None:
                raise ReqError(
                    f"rule {rid}: contradictory termination: give exactly one of "
                    "'count' or 'untilLocal', not both",
                    rid,
                    "CONTRADICTORY_TERMINATION",
                )
            raise ReqError(
                f"rule {rid}: unbounded rule: exactly one of 'count' or "
                "'untilLocal' is required",
                rid,
                "UNBOUNDED_RULE",
            )

        until_local = None
        if r.untilLocal is not None:
            until_local = parse_naive(r.untilLocal, "untilLocal", rid)
            if until_local < start_local:
                raise ReqError(
                    f"rule {rid}: untilLocal ({until_local.isoformat()}) precedes "
                    f"startLocal ({start_local.isoformat()})",
                    rid,
                    "CONTRADICTORY_TERMINATION",
                )

        by_weekday: tuple[int, ...] = ()
        if r.frequency == WEEKLY:
            if not r.byWeekday:
                raise ReqError(
                    f"rule {rid}: WEEKLY rule requires non-empty byWeekday "
                    "(1=Monday .. 7=Sunday)",
                    rid,
                    "MISSING_WEEKDAYS",
                )
            if len(set(r.byWeekday)) != len(r.byWeekday):
                raise ReqError(
                    f"rule {rid}: byWeekday entries must be distinct",
                    rid,
                    "DUPLICATE_WEEKDAY",
                )
            if any(not 1 <= d <= 7 for d in r.byWeekday):
                raise ReqError(
                    f"rule {rid}: byWeekday entries must be within 1..7",
                    rid,
                    "INVALID_WEEKDAY",
                )
            weekday_set = set(r.byWeekday)
            # ISO weekday of startLocal: Monday=1 .. Sunday=7. Requiring the
            # anchor to belong to the set removes any ambiguity about where
            # the weekly stride begins.
            if start_local.isoweekday() not in weekday_set:
                names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
                raise ReqError(
                    f"rule {rid}: startLocal weekday "
                    f"({names[start_local.isoweekday() - 1]}) must be included "
                    "in byWeekday",
                    rid,
                    "START_WEEKDAY_NOT_IN_SET",
                )
            by_weekday = tuple(sorted(weekday_set))
        elif r.byWeekday is not None:
            raise ReqError(
                f"rule {rid}: byWeekday is only allowed for WEEKLY rules",
                rid,
                "UNEXPECTED_WEEKDAYS",
            )

        rules.append(
            Rule(
                id=rid,
                start_local=start_local,
                duration_minutes=r.durationMinutes,
                frequency=r.frequency,
                interval=r.interval,
                count=r.count,
                until_local=until_local,
                by_weekday=by_weekday,
            )
        )

    return tz, req.ambiguousTime, range_start, range_end, rules


def format_offset(delta) -> str:
    """Format a timedelta UTC offset as ``±HH:MM[:SS]``."""
    total = int(delta.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if seconds:
        return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{sign}{hours:02d}:{minutes:02d}"


def format_naive(dt: datetime) -> str:
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if dt.microsecond:
        base += f".{dt.microsecond:06d}".rstrip("0")
    return base


def format_utc(dt: datetime) -> str:
    dt = dt.astimezone(UTC)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if dt.microsecond:
        base += f".{dt.microsecond:06d}".rstrip("0")
    return base + "Z"


app = FastAPI(title="Maintenance Window Expander", version="1.0.0")


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_: Request, exc: RequestValidationError):
    """Surface schema errors with the offending rule number when available."""
    rule_id = None
    for err in exc.errors():
        loc = err.get("loc", ())
        if "rules" in loc:
            idx = loc.index("rules") + 1
            if idx < len(loc) and isinstance(loc[idx], int):
                try:
                    raw = exc.body if isinstance(exc.body, dict) else {}
                    rule = raw.get("rules", [])[loc[idx]]
                    if isinstance(rule, dict) and isinstance(rule.get("id"), int):
                        rule_id = rule["id"]
                except (AttributeError, IndexError, TypeError):
                    pass
                if rule_id is None:
                    rule_id = loc[idx]
                break
    first = exc.errors()[0] if exc.errors() else {}
    detail = first.get("msg", "invalid request")
    loc_str = ".".join(str(p) for p in first.get("loc", ()))
    message = f"{loc_str}: {detail}" if loc_str else detail
    return error("VALIDATION_ERROR", message, status=422, rule_id=rule_id)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/maintenance-windows/expand")
def expand_windows(req: ExpandRequest):
    try:
        tz, ambiguous, range_start, range_end, rules = validate_and_build(req)
        windows = expand(rules, tz, ambiguous, range_start, range_end)
    except ReqError as exc:
        return error(exc.code, exc.message, rule_id=exc.rule_id)
    except ExpansionLimitExceeded as exc:
        return error(
            "EXPANSION_LIMIT_EXCEEDED",
            f"{exc}; narrow the range or the termination condition",
            rule_id=exc.rule_id,
        )

    return {
        "timeZone": req.timeZone,
        "rangeStartUtc": format_utc(range_start),
        "rangeEndUtc": format_utc(range_end),
        "ambiguousTime": ambiguous,
        "count": len(windows),
        "windows": [
            {
                "ruleId": w.rule_id,
                "occurrence": w.occurrence,
                "frequency": w.frequency,
                "durationMinutes": w.duration_minutes,
                "startLocal": format_naive(w.start_local),
                "startUtc": format_utc(w.start_utc),
                "endUtc": format_utc(w.end_utc),
                "utcOffset": format_offset(w.utc_offset),
                "resolution": w.resolution,
            }
            for w in windows
        ],
    }
