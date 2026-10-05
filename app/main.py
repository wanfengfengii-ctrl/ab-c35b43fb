"""HTTP API for expanding local recurring maintenance windows into UTC."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .expander import ExpansionError, Rule, expand_rules
from .models import ExpandRequest

app = FastAPI(title="Maintenance Window Expander", version="1.0.0")


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, rule_id: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.rule_id = rule_id


def _error_body(
    code: str,
    message: str,
    rule_id: Optional[str] = None,
    details: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if rule_id is not None:
        error["ruleId"] = rule_id
    if details is not None:
        error["details"] = details
    return {"error": error}


@app.exception_handler(ApiError)
async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_body(exc.code, exc.message, exc.rule_id),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    body: Any = None
    try:
        body = await request.json()
    except Exception:
        body = None

    details: List[Dict[str, Any]] = []
    for err in exc.errors():
        loc = [str(part) for part in err.get("loc", []) if part != "body"]
        detail: Dict[str, Any] = {"loc": loc, "msg": str(err.get("msg", ""))}
        # Attach the offending rule id when the error sits inside rules[i].
        if isinstance(body, dict) and "rules" in loc:
            i = loc.index("rules")
            if i + 1 < len(loc):
                try:
                    rule_id = body["rules"][int(loc[i + 1])].get("id")
                except Exception:
                    rule_id = None
                if rule_id is not None:
                    detail["ruleId"] = rule_id
        details.append(detail)

    message = "; ".join(f"{'.'.join(d['loc']) or 'request'}: {d['msg']}" for d in details[:5])
    return JSONResponse(
        status_code=422,
        content=_error_body("VALIDATION_ERROR", message or "invalid request", details=details),
    )


def _format_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _format_offset(offset: timedelta) -> str:
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/api/maintenance-windows/expand")
def expand(request: ExpandRequest) -> Dict[str, Any]:
    try:
        tz = ZoneInfo(request.timezone)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise ApiError(
            status_code=400,
            code="UNKNOWN_TIMEZONE",
            message=f"unknown IANA timezone: {request.timezone!r}",
        )

    rules = [
        Rule(
            id=r.id,
            start_local=r.startLocal,
            duration_minutes=r.durationMinutes,
            frequency=r.frequency,
            interval=r.interval,
            weekdays=tuple(r.weekdays) if r.weekdays else None,
            count=r.count,
            until=r.until,
        )
        for r in request.rules
    ]
    range_start = request.rangeStartUtc.astimezone(timezone.utc)
    range_end = request.rangeEndUtc.astimezone(timezone.utc)

    try:
        windows = expand_rules(rules, tz, request.ambiguousTimePolicy, range_start, range_end)
    except ExpansionError as exc:
        raise ApiError(status_code=400, code=exc.code, message=exc.message, rule_id=exc.rule_id)

    return {
        "timezone": request.timezone,
        "rangeStartUtc": _format_utc(range_start),
        "rangeEndUtc": _format_utc(range_end),
        "ambiguousTimePolicy": request.ambiguousTimePolicy,
        "total": len(windows),
        "windows": [
            {
                "ruleId": w.rule_id,
                "localStart": w.local_start.isoformat(),
                "utcStart": _format_utc(w.utc_start),
                "utcEnd": _format_utc(w.utc_end),
                "utcOffset": _format_offset(w.utc_offset),
                "resolution": w.resolution,
            }
            for w in windows
        ],
    }
