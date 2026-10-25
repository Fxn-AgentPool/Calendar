"""Turn stored calendar objects into concrete occurrences in a time range."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import recurring_ical_events

from calapp.common.caldav import CalendarObject

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")


def safe_color(value: str | None) -> str | None:
    """Only plain hex colours are passed through to the page."""
    return value[:7] if value and _COLOR.match(value) else None


@dataclass
class Occurrence:
    uid: str
    title: str
    start: datetime | date  # aware datetime in the display zone, or date for all-day
    end: datetime | date  # exclusive
    all_day: bool
    transparent: bool  # TRANSP:TRANSPARENT ("free"), doesn't block time
    location: str | None
    description: str | None
    ingest_job: str | None
    # Ingest review state: "UNCERTAIN", "CHECKED" or None (see docs/DESIGN.md §8).
    ingest_status: str | None = None
    calendar: str = ""  # display name
    calendar_index: int = 0

    def range(self, tz: ZoneInfo) -> tuple[datetime, datetime]:
        if self.all_day:
            return (
                datetime.combine(self.start, datetime.min.time(), tz),  # type: ignore[arg-type]
                datetime.combine(self.end, datetime.min.time(), tz),  # type: ignore[arg-type]
            )
        return self.start, self.end  # type: ignore[return-value]

    def days(self, tz: ZoneInfo) -> list[date]:
        """Local dates this occurrence touches."""
        start, end = self.range(tz)
        first = start.date()
        last = (end - timedelta(microseconds=1)).date() if end > start else first
        return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _text(component, name: str) -> str | None:
    value = component.get(name)
    return str(value).strip() or None if value is not None else None


def _as_local(value: datetime | date, tz: ZoneInfo) -> datetime | date:
    if isinstance(value, datetime):
        # Floating times (no zone) are read as local time.
        return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
    return value


def expand(objects: list[CalendarObject], start: datetime, end: datetime, tz: ZoneInfo) -> list[Occurrence]:
    """All VEVENT occurrences overlapping [start, end), sorted by start."""
    result = []
    for obj in objects:
        try:
            components = recurring_ical_events.of(obj.calendar).between(start, end)
        except Exception:
            continue  # one broken object should not hide the rest
        for comp in components:
            if comp.name != "VEVENT" or comp.get("dtstart") is None:
                continue
            if str(comp.get("status", "")).upper() == "CANCELLED":
                continue
            dtstart = _as_local(comp["dtstart"].dt, tz)
            all_day = not isinstance(dtstart, datetime)
            if comp.get("dtend") is not None:
                dtend = _as_local(comp["dtend"].dt, tz)
            elif comp.get("duration") is not None:
                dtend = dtstart + comp["duration"].dt
            else:
                dtend = dtstart + timedelta(days=1) if all_day else dtstart
            job = comp.get("x-ingest-job")
            result.append(Occurrence(
                uid=str(comp.get("uid", "")),
                title=_text(comp, "summary") or "(no title)",
                start=dtstart,
                end=dtend,
                all_day=all_day,
                transparent=str(comp.get("transp", "")).upper() == "TRANSPARENT",
                location=_text(comp, "location"),
                description=_text(comp, "description"),
                ingest_job=str(job) if job is not None else None,
                ingest_status=str(comp["x-ingest-status"]).upper() if comp.get("x-ingest-status") else None,
            ))
    result.sort(key=lambda o: (o.range(tz)[0], not o.all_day, o.title.casefold()))
    return result
