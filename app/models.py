"""Request/response schemas for the maintenance-window expansion API."""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Weekday = Literal["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
Frequency = Literal["DAILY", "WEEKLY"]
AmbiguousTimePolicy = Literal["earlier", "later"]


class RuleIn(BaseModel):
    """One numbered local recurring rule."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    startLocal: datetime
    durationMinutes: int = Field(gt=0)
    frequency: Frequency
    interval: int = Field(ge=1, le=30)
    weekdays: Optional[List[Weekday]] = None
    count: Optional[int] = Field(default=None, ge=1)
    until: Optional[datetime] = None

    @field_validator("startLocal", "until")
    @classmethod
    def _must_be_naive(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None and v.tzinfo is not None:
            raise ValueError("must be a local wall time without timezone offset")
        return v

    @model_validator(mode="after")
    def _check_rule(self) -> "RuleIn":
        label = f"rule {self.id!r}"
        if (self.count is None) == (self.until is None):
            raise ValueError(
                f"{label}: exactly one of 'count' or 'until' must be given; "
                "termination must be bounded and unambiguous"
            )
        if self.frequency == "WEEKLY":
            if not self.weekdays:
                raise ValueError(f"{label}: WEEKLY frequency requires a non-empty 'weekdays' set")
            if len(set(self.weekdays)) != len(self.weekdays):
                raise ValueError(f"{label}: 'weekdays' must be distinct")
        elif self.weekdays is not None:
            raise ValueError(f"{label}: 'weekdays' is only allowed with WEEKLY frequency")
        if self.until is not None and self.until < self.startLocal:
            raise ValueError(f"{label}: 'until' must not be earlier than 'startLocal'")
        return self


class ExpandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str = Field(min_length=1, max_length=64)
    rangeStartUtc: datetime
    rangeEndUtc: datetime
    ambiguousTimePolicy: AmbiguousTimePolicy = "earlier"
    rules: List[RuleIn]

    @field_validator("rangeStartUtc", "rangeEndUtc")
    @classmethod
    def _must_be_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("must be an explicit UTC instant (ISO 8601 with offset, e.g. '...Z')")
        return v

    @model_validator(mode="after")
    def _check_request(self) -> "ExpandRequest":
        if not self.rangeStartUtc < self.rangeEndUtc:
            raise ValueError("rangeStartUtc must be earlier than rangeEndUtc (half-open range)")
        if not 1 <= len(self.rules) <= 20:
            raise ValueError(f"request must contain between 1 and 20 rules, got {len(self.rules)}")
        seen: set = set()
        duplicates: List[str] = []
        for rule in self.rules:
            if rule.id in seen and rule.id not in duplicates:
                duplicates.append(rule.id)
            seen.add(rule.id)
        if duplicates:
            raise ValueError(
                "rule ids must be unique; duplicated: " + ", ".join(repr(d) for d in duplicates)
            )
        return self
