"""Request/response shapes of the ingest API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    received_at: datetime | None = None
    source: str | None = Field(default=None, pattern=r"^[a-z_-]{1,20}$")
    timezone: str | None = Field(default=None, max_length=64)

    @field_validator("timezone")
    @classmethod
    def _valid_zone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except Exception as e:
            raise ValueError("not a valid IANA time zone") from e
        return value


Outcome = Literal["added", "duplicate", "conflict", "invalid", "error"]


class EventResult(BaseModel):
    outcome: Outcome
    title: str
    start: datetime | date | None = None
    end: datetime | date | None = None
    all_day: bool = False
    uid: str | None = None
    conflicts_with: list[str] | None = None
    reason: str | None = None
    # Set when the confidence check ran: the probability that the time is right.
    confidence: float | None = None
    uncertain: bool = False


JobStatus = Literal["pending", "running", "done", "failed"]


class JobResult(BaseModel):
    job_id: str
    status: JobStatus
    ok: bool = False
    summary: str | None = None
    events: list[EventResult] = []
    error: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
