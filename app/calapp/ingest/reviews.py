"""Queue of uncertain events, re-checked by a helper with a bigger model.

The helper (clients/review_helper.py on the desktop) pulls an item, runs the
review prompt on its own LLM and posts the corrected event back. Applying it
is the only place the ingest service changes an existing event, and only one
it created itself that is still marked uncertain and unchanged.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from calapp.common.caldav import CalDAVError
from calapp.ingest.llm import REVIEW_SCHEMA, build_review_messages
from calapp.ingest.pipeline import (
    UNCERTAIN_PREFIX,
    Event,
    Invalid,
    Pipeline,
    describe,
    normalize,
    to_ical,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL,          -- pending, claimed, done
    outcome TEXT,                  -- confirmed, corrected, kept, skipped, failed
    detail TEXT,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    finished_at TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_status ON reviews(status, created_at);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | date) -> str:
    return value.isoformat()


def _parse(value: str) -> datetime | date:
    return datetime.fromisoformat(value) if "T" in value else date.fromisoformat(value)


@dataclass
class ReviewItem:
    id: str
    job_id: str
    status: str
    payload: dict

    @property
    def reference(self) -> datetime:
        return datetime.fromisoformat(self.payload["reference"])

    def event(self) -> Event:
        e = self.payload["event"]
        start, end = _parse(e["start"]), _parse(e["end"])
        if e.get("tz") and isinstance(start, datetime):
            # Back to the named zone (the ISO string only keeps the offset).
            zone = ZoneInfo(e["tz"])
            start, end = start.astimezone(zone), end.astimezone(zone)  # type: ignore[union-attr]
        return Event(e["title"], e["all_day"], start, end, e["location"], e["notes"])


class ReviewQueue:
    def __init__(self, path: str | Path, lease: timedelta = timedelta(minutes=10)):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()
        self.lease = lease

    def add(self, *, job_id: str, text: str, reference: datetime, timezone: str, source: str | None,
            raw: dict, event: Event, confidence: float | None) -> str:
        payload = {
            "text": text, "reference": reference.isoformat(), "timezone": timezone, "source": source,
            "raw": raw, "confidence": confidence, "uid": event.uid,
            "event": {"title": event.title, "all_day": event.all_day, "start": _iso(event.start),
                      "end": _iso(event.end), "tz": getattr(getattr(event.start, "tzinfo", None), "key", None),
                      "location": event.location, "notes": event.notes},
        }
        review_id = secrets.token_urlsafe(12)
        with self._lock:
            self._db.execute(
                "INSERT INTO reviews (id, job_id, status, created_at, payload) VALUES (?, ?, 'pending', ?, ?)",
                (review_id, job_id, _now().isoformat(), json.dumps(payload, ensure_ascii=False)),
            )
        return review_id

    def claim(self) -> ReviewItem | None:
        """The oldest open item (or one whose lease ran out), marked as claimed."""
        stale = (_now() - self.lease).isoformat()
        with self._lock:
            row = self._db.execute(
                "SELECT id, job_id, status, payload FROM reviews WHERE status = 'pending' "
                "OR (status = 'claimed' AND claimed_at < ?) ORDER BY created_at LIMIT 1",
                (stale,),
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                "UPDATE reviews SET status = 'claimed', claimed_at = ? WHERE id = ?", (_now().isoformat(), row[0])
            )
        return ReviewItem(row[0], row[1], "claimed", json.loads(row[3]))

    def get(self, review_id: str) -> ReviewItem | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id, job_id, status, payload FROM reviews WHERE id = ?", (review_id,)
            ).fetchone()
        return ReviewItem(row[0], row[1], row[2], json.loads(row[3])) if row else None

    def finish(self, review_id: str, outcome: str, detail: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE reviews SET status = 'done', outcome = ?, detail = ?, finished_at = ? WHERE id = ?",
                (outcome, detail, _now().isoformat(), review_id),
            )

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT COALESCE(outcome, status), COUNT(*) FROM reviews GROUP BY 1"
            ).fetchall()
        return {k: v for k, v in rows}

    def expire(self, ttl: timedelta) -> int:
        """Forget items (and their text) older than ``ttl``, finished or not."""
        cutoff = (_now() - ttl).isoformat()
        with self._lock:
            return self._db.execute("DELETE FROM reviews WHERE created_at < ?", (cutoff,)).rowcount


def task_for(item: ReviewItem) -> dict:
    """What the helper gets: ready-to-run chat messages and the output schema."""
    p = item.payload
    messages = build_review_messages(p["text"], item.reference, p["timezone"], p["source"], p["raw"])
    return {"id": item.id, "messages": messages, "schema": REVIEW_SCHEMA}


def _same_moment(a, b) -> bool:
    if isinstance(a, datetime) and isinstance(b, datetime):
        return a == b  # aware datetimes compare by instant
    if isinstance(a, datetime) or isinstance(b, datetime):
        return False
    return a == b


def apply_result(pipeline: Pipeline, item: ReviewItem, answer: dict) -> tuple[str, str]:
    """Apply a helper's answer. Returns (outcome, detail)."""
    p = item.payload
    tz = ZoneInfo(p["timezone"])
    original = item.event()
    uid = p["uid"]
    path = f"{pipeline.calendar_path.rstrip('/')}/{uid}.ics"
    try:
        current = pipeline.calendar.get(path)
    except CalDAVError as e:
        raise RuntimeError(f"calendar unreachable: {e}") from e
    if current is None:
        return "skipped", "the event was deleted"
    cal, etag = current
    vevents = cal.walk("VEVENT")
    if not vevents:
        return "skipped", "the stored object has no event"
    vevent = vevents[0]
    unchanged = (
        str(vevent.get("x-ingest-status", "")).upper() == "UNCERTAIN"
        and str(vevent.get("summary", "")) == UNCERTAIN_PREFIX + original.title
        and vevent.get("dtstart") is not None and _same_moment(vevent["dtstart"].dt, original.start)
        and vevent.get("dtend") is not None and _same_moment(vevent["dtend"].dt, original.end)
    )
    if not unchanged:
        return "skipped", "the event was changed in the calendar since it was added"

    raw = answer.get("event") if isinstance(answer, dict) else None
    if not isinstance(raw, dict):
        return "failed", "answer has no event"
    try:
        corrected = normalize(raw, reference=item.reference, user_tz=tz, default_minutes=pipeline.default_minutes)
    except Invalid as e:
        return "failed", f"answer is not a valid event: {e}"

    was = describe(original.start, original.end, original.all_day, tz)
    if (corrected.all_day == original.all_day and _same_moment(corrected.start, original.start)
            and _same_moment(corrected.end, original.end)):
        ical = to_ical(original, item.job_id, uid=uid, checked=True, note="Checked by desktop model.")
        if not pipeline.calendar.update(path, ical, etag):
            return "skipped", "the event changed while it was being confirmed"
        return "confirmed", was

    now = describe(corrected.start, corrected.end, corrected.all_day, tz)
    _, conflicts = pipeline.find_conflicts(corrected, ignore_uid=uid)
    if conflicts:
        note = f"Desktop check suggests {now}; not applied because it conflicts with {', '.join(conflicts)}."
        ical = to_ical(original, item.job_id, uid=uid, confidence=p.get("confidence") or 0.0, note=note)
        if not pipeline.calendar.update(path, ical, etag):
            return "skipped", "the event changed while it was being updated"
        return "kept", note
    ical = to_ical(corrected, item.job_id, uid=uid, checked=True, note=f"Corrected by desktop model (was {was}).")
    if not pipeline.calendar.update(path, ical, etag):
        return "skipped", "the event changed while it was being corrected"
    return "corrected", f"{was} -> {now}"
