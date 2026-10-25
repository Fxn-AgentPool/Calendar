"""Job storage in SQLite. Jobs (including their input text) expire after a TTL."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from calapp.ingest.models import EventResult, IngestRequest, JobResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    input TEXT,
    events TEXT,
    summary TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class JobStore:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def create(self, request: IngestRequest) -> str:
        job_id = secrets.token_urlsafe(12)
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs (id, status, created_at, input) VALUES (?, 'pending', ?, ?)",
                (job_id, _now().isoformat(), request.model_dump_json()),
            )
        return job_id

    def input(self, job_id: str) -> IngestRequest | None:
        with self._lock:
            row = self._db.execute("SELECT input FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return IngestRequest.model_validate_json(row[0]) if row and row[0] else None

    def mark_running(self, job_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))

    def finish(self, job_id: str, events: list[EventResult], summary: str) -> None:
        payload = json.dumps([e.model_dump(mode="json") for e in events])
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status = 'done', finished_at = ?, events = ?, summary = ? WHERE id = ?",
                (_now().isoformat(), payload, summary, job_id),
            )

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status = 'failed', finished_at = ?, error = ?, summary = ? WHERE id = ?",
                (_now().isoformat(), error, f"Failed: {error}", job_id),
            )

    def get(self, job_id: str) -> JobResult | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id, status, created_at, finished_at, events, summary, error FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if not row:
            return None
        events = [EventResult.model_validate(e) for e in json.loads(row[4])] if row[4] else []
        ok = row[1] == "done" and bool(events) and all(e.outcome in ("added", "duplicate") for e in events)
        return JobResult(
            job_id=row[0], status=row[1], ok=ok, created_at=row[2], finished_at=row[3],
            events=events, summary=row[5], error=row[6],
        )

    def unfinished(self) -> list[str]:
        """Jobs to (re)queue after a restart, oldest first."""
        with self._lock:
            self._db.execute("UPDATE jobs SET status = 'pending' WHERE status = 'running'")
            rows = self._db.execute(
                "SELECT id FROM jobs WHERE status = 'pending' ORDER BY created_at"
            ).fetchall()
        return [r[0] for r in rows]

    def expire(self, ttl: timedelta) -> int:
        cutoff = (_now() - ttl).isoformat()
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM jobs WHERE created_at < ? AND status IN ('done', 'failed')", (cutoff,)
            )
        return cur.rowcount
