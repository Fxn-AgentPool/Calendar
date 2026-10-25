"""Test fixtures: a real Radicale (repo config + rights) and a fake LLM."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import bcrypt
import httpx
import pytest

from calapp.ingest.llm import ExtractionResult, LLMError

REPO = Path(__file__).resolve().parents[2]
OWNER = "me"
PASSWORDS = {OWNER: "owner-password", "ingest": "ingest-password", "agenda": "agenda-password"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def radicale(tmp_path_factory) -> str:
    """Starts Radicale with radicale/config and radicale/rights.template."""
    tmp = tmp_path_factory.mktemp("radicale")
    users = tmp / "users"
    users.write_text(
        "".join(
            f"{u}:{bcrypt.hashpw(p.encode(), bcrypt.gensalt(4)).decode()}\n" for u, p in PASSWORDS.items()
        )
    )
    rights = tmp / "rights"
    rights.write_text((REPO / "radicale" / "rights.template").read_text().replace("@OWNER@", OWNER))
    storage = tmp / "collections"
    for account in ("ingest", "agenda"):
        (storage / "collection-root" / account).mkdir(parents=True)
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "radicale",
            "--config", str(REPO / "radicale" / "config"),
            "--server-hosts", f"127.0.0.1:{port}",
            "--auth-htpasswd-filename", str(users),
            "--auth-delay", "0",
            "--rights-file", str(rights),
            "--storage-filesystem-folder", str(storage),
            "--logging-level", "warning",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            # The owner's first login creates the "personal" calendar.
            if httpx.request("PROPFIND", f"{url}/{OWNER}/", auth=(OWNER, PASSWORDS[OWNER]),
                             headers={"Depth": "0"}).status_code == 207:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("Radicale did not start")
    yield url
    proc.terminate()
    proc.wait(5)


@pytest.fixture
def clean_calendar(radicale):
    """Empties the owner's calendars before a test."""
    owner = httpx.Client(base_url=radicale, auth=(OWNER, PASSWORDS[OWNER]))
    for calendar in ("personal", "work"):
        owner.request("DELETE", f"/{OWNER}/{calendar}/")
    owner.request(
        "MKCOL", f"/{OWNER}/personal/",
        content=b'<?xml version="1.0"?><d:mkcol xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        b"<d:set><d:prop><d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
        b"<d:displayname>Personal</d:displayname></d:prop></d:set></d:mkcol>",
    )
    yield owner
    owner.close()


class FakeLLM:
    """Returns canned events; records what it was asked."""

    def __init__(self, events: list[dict] | None = None, error: str | None = None, delay: float = 0):
        self.events = events or []
        self.error = error
        self.delay = delay
        self.calls: list[dict] = []

    def extract(self, text: str, reference: datetime, timezone: str, source: str | None = None):
        self.calls.append({"text": text, "reference": reference, "timezone": timezone, "source": source})
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise LLMError(self.error)
        return ExtractionResult(events=list(self.events), prompt_tokens=10, completion_tokens=5, seconds=0.01)

    def healthy(self) -> bool:
        return self.error is None


def raw_event(title="Dentist", start="2026-10-27T14:00", end="2026-10-27T14:45", **extra) -> dict:
    event = {"title": title, "all_day": False, "start": start, "end": end,
             "timezone": None, "location": None, "notes": None}
    event.update(extra)
    return event
