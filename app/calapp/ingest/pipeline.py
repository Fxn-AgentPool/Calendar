"""From raw text to calendar events: clean, extract, validate, write."""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import icalendar

from calapp.common.caldav import CalDAVClient, CalDAVError
from calapp.ingest import textprep
from calapp.ingest.llm import LLMClient, LLMError
from calapp.ingest.models import EventResult, IngestRequest

log = logging.getLogger("calapp.ingest")

CATEGORY = "llm-ingest"
MAX_TIMED_DURATION = timedelta(hours=24)
MAX_ALL_DAY_DAYS = 31
PAST_WINDOW = timedelta(days=1)
FUTURE_WINDOW = timedelta(days=730)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


class Invalid(Exception):
    """An extracted event failed validation; the message says why."""


@dataclass
class Event:
    title: str
    all_day: bool
    start: datetime | date  # aware datetime, or date for all-day events
    end: datetime | date  # exclusive: for all-day events the day after the last day
    location: str | None
    notes: str | None

    @property
    def uid(self) -> str:
        def key(v: datetime | date) -> str:
            return v.astimezone(timezone.utc).isoformat() if isinstance(v, datetime) else v.isoformat()

        title = " ".join(self.title.casefold().split())
        digest = hashlib.sha256(f"{title}|{key(self.start)}|{key(self.end)}".encode()).hexdigest()
        return f"ingest-{digest[:32]}"

    def range(self, tz: ZoneInfo) -> tuple[datetime, datetime]:
        """Start and end as aware datetimes (all-day events span whole days in ``tz``)."""
        if isinstance(self.start, datetime):
            return self.start, self.end  # type: ignore[return-value]
        return (
            datetime.combine(self.start, datetime.min.time(), tz),
            datetime.combine(self.end, datetime.min.time(), tz),  # type: ignore[arg-type]
        )


def _parse(value: object) -> date | datetime:
    if not isinstance(value, str):
        raise Invalid("missing date")
    value = value.strip()
    try:
        if _DATETIME_RE.match(value):
            return datetime.fromisoformat(value)
        if _DATE_RE.match(value):
            return date.fromisoformat(value)
    except ValueError as e:
        raise Invalid(f"impossible date {value!r}") from e
    raise Invalid(f"unreadable date {value!r}")


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] or None


def normalize(raw: dict, *, reference: datetime, user_tz: ZoneInfo, default_minutes: int) -> Event:
    """Validate one event from the LLM and turn it into an :class:`Event`."""
    title = _text(raw.get("title"), 200)
    if not title:
        raise Invalid("missing title")

    tz = user_tz
    if raw.get("timezone"):
        try:
            tz = ZoneInfo(str(raw["timezone"]))
        except Exception:
            tz = user_tz  # unknown zone name: fall back to the user's

    start = _parse(raw.get("start"))
    end = _parse(raw["end"]) if raw.get("end") else None
    all_day = bool(raw.get("all_day")) or not isinstance(start, datetime)

    if all_day:
        start_day = start.date() if isinstance(start, datetime) else start
        if end is None:
            last_day = start_day
        else:
            last_day = end.date() if isinstance(end, datetime) else end
        if last_day < start_day:
            raise Invalid("ends before it starts")
        if (last_day - start_day).days >= MAX_ALL_DAY_DAYS:
            raise Invalid(f"longer than {MAX_ALL_DAY_DAYS} days")
        event = Event(title, True, start_day, last_day + timedelta(days=1), None, None)
        first_moment = datetime.combine(start_day, datetime.min.time(), user_tz)
    else:
        assert isinstance(start, datetime)
        start_dt = start.replace(tzinfo=tz)
        if isinstance(end, datetime):
            end_dt = end.replace(tzinfo=tz)
            if end_dt <= start_dt:
                # "22:00-01:00": ends the next day.
                if end.date() == start.date() and end_dt + timedelta(days=1) > start_dt:
                    end_dt += timedelta(days=1)
                else:
                    raise Invalid("ends before it starts")
        else:
            end_dt = start_dt + timedelta(minutes=default_minutes)
        if end_dt - start_dt > MAX_TIMED_DURATION:
            raise Invalid("longer than 24 hours")
        event = Event(title, False, start_dt, end_dt, None, None)
        first_moment = start_dt

    if first_moment < reference - PAST_WINDOW:
        raise Invalid("in the past")
    if first_moment > reference + FUTURE_WINDOW:
        raise Invalid("more than two years ahead")

    event.location = _text(raw.get("location"), 200)
    event.notes = _text(raw.get("notes"), 2000)
    return event


def to_ical(event: Event, job_id: str) -> bytes:
    cal = icalendar.Calendar()
    cal.add("prodid", "-//calapp//ingest//EN")
    cal.add("version", "2.0")
    vevent = icalendar.Event()
    vevent.add("uid", event.uid)
    vevent.add("dtstamp", datetime.now(timezone.utc))
    vevent.add("summary", event.title)
    vevent.add("dtstart", event.start)
    vevent.add("dtend", event.end)
    if event.all_day:
        vevent.add("transp", "TRANSPARENT")
    if event.location:
        vevent.add("location", event.location)
    description = f"{event.notes}\n\n" if event.notes else ""
    vevent.add("description", f"{description}Added from text by calendar ingest (job {job_id}).")
    vevent.add("categories", [CATEGORY])
    vevent.add("x-ingest-job", job_id)
    cal.add_component(vevent)
    cal.add_missing_timezones()
    return cal.to_ical()


def describe(start: datetime | date, end: datetime | date, all_day: bool, tz: ZoneInfo) -> str:
    """Short human-readable time, e.g. "Tue 27 Oct 14:00–14:45"."""
    if all_day:
        last = end - timedelta(days=1)  # type: ignore[operator]
        if last == start:
            return f"{start:%a %d %b} (all day)"
        return f"{start:%a %d %b} – {last:%a %d %b}"
    s = start.astimezone(tz)  # type: ignore[union-attr]
    e = end.astimezone(tz)  # type: ignore[union-attr]
    if s.date() == e.date():
        return f"{s:%a %d %b %H:%M}–{e:%H:%M}"
    return f"{s:%a %d %b %H:%M} – {e:%a %d %b %H:%M}"


def summarize(results: list[EventResult], tz: ZoneInfo) -> str:
    lines = []
    for r in results:
        when = describe(r.start, r.end, r.all_day, tz) if r.start and r.end else ""
        if r.outcome == "added":
            lines.append(f"Added: {r.title}, {when}")
        elif r.outcome == "duplicate":
            lines.append(f"Already in calendar: {r.title}, {when}")
        elif r.outcome == "conflict":
            others = ", ".join(r.conflicts_with or [])
            lines.append(f"Not added (conflicts with {others}): {r.title}, {when}")
        elif r.outcome == "invalid":
            lines.append(f"Skipped ({r.reason}): {r.title}")
        else:
            lines.append(f"Error ({r.reason}): {r.title}")
    return "\n".join(lines)


class Pipeline:
    def __init__(
        self,
        llm: LLMClient,
        calendar: CalDAVClient,
        calendar_path: str,
        user_tz: ZoneInfo,
        default_minutes: int = 60,
        max_text_chars: int = 20_000,
        llm_retry_delays: tuple[float, ...] = (10, 30, 60),
    ):
        self.llm = llm
        self.calendar = calendar
        self.calendar_path = calendar_path
        self.user_tz = user_tz
        self.default_minutes = default_minutes
        self.max_text_chars = max_text_chars
        self.llm_retry_delays = llm_retry_delays

    def find_conflicts(self, event: Event) -> tuple[bool, list[str]]:
        """Returns (exact duplicate already stored, titles of overlapping events).

        Stub until milestone 6: never reports anything.
        """
        return False, []

    def _extract(self, text: str, reference: datetime, tz_name: str, source: str | None) -> list[dict]:
        # Retry while the LLM is restarting or still loading the model.
        for delay in (*self.llm_retry_delays, None):
            try:
                return self.llm.extract(text, reference, tz_name, source).events
            except LLMError as e:
                if e.code not in ("llm_unreachable", "llm_error") or delay is None:
                    raise
                log.info("LLM not ready (%s), retrying in %ss", e.code, delay)
                time.sleep(delay)
        raise AssertionError("unreachable")

    def run(self, job_id: str, request: IngestRequest) -> list[EventResult]:
        """Process one job. Raises LLMError if extraction fails."""
        tz = ZoneInfo(request.timezone) if request.timezone else self.user_tz
        reference = request.received_at or datetime.now(tz)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=tz)
        reference = reference.astimezone(tz)

        text = textprep.clean(request.text, self.max_text_chars)
        raw_events = self._extract(text, reference, str(tz), request.source)

        results: list[EventResult] = []
        for raw in raw_events:
            title = _text(raw.get("title"), 200) or "(untitled)"
            try:
                event = normalize(raw, reference=reference, user_tz=tz, default_minutes=self.default_minutes)
            except Invalid as e:
                results.append(EventResult(outcome="invalid", title=title, reason=str(e)))
                continue
            result = EventResult(
                outcome="added", title=event.title, start=event.start, end=event.end,
                all_day=event.all_day, uid=event.uid,
            )
            try:
                duplicate, conflicts = self.find_conflicts(event)
                if duplicate:
                    result.outcome = "duplicate"
                elif conflicts:
                    result.outcome = "conflict"
                    result.conflicts_with = conflicts
                elif not self.calendar.create(self.calendar_path, event.uid, to_ical(event, job_id)):
                    result.outcome = "duplicate"
            except CalDAVError as e:
                log.warning("job %s: calendar error: %s", job_id, e)
                result.outcome = "error"
                result.reason = "calendar_unreachable"
            results.append(result)
        return results
