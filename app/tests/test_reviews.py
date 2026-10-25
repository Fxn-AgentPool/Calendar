"""Confidence check, uncertain events, review queue and the helper endpoints."""

import hashlib
import json
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

import icalendar
import pytest
from fastapi.testclient import TestClient

from calapp.common.caldav import CalDAVClient
from calapp.ingest.checker import Checker, CheckerError, build_request
from calapp.ingest.main import create_app
from calapp.ingest.pipeline import Pipeline
from calapp.ingest.reviews import ReviewQueue
from conftest import OWNER, PASSWORDS, FakeLLM, raw_event
from test_api import AUTH, BODY, settings

BERLIN = ZoneInfo("Europe/Berlin")
HELPER_KEY = "helper-key-0123456789abcdef0123456789abc"
HELPER = {"Authorization": f"Bearer {HELPER_KEY}"}
CAL = f"/{OWNER}/personal/"


class FakeChecker:
    def __init__(self, confidence=0.9, error=False):
        self.value, self.error, self.calls = confidence, error, []

    def confidence(self, text, reference, title, start):
        self.calls.append((title, start))
        if self.error:
            raise CheckerError("down")
        return self.value

    def healthy(self):
        return not self.error


@pytest.fixture
def make(radicale, tmp_path, clean_calendar):
    """Builds an app with a fake LLM and checker; returns (client, owner_http)."""
    clients = []

    def build(events=None, confidence=0.2, checker_error=False, lease_minutes=10, helper=True):
        s = settings(radicale, tmp_path, helper_key_hash=hashlib.sha256(HELPER_KEY.encode()).hexdigest()
                     if helper else None)
        llm = FakeLLM(events or [raw_event()])
        calendar = CalDAVClient(radicale, "ingest", PASSWORDS["ingest"])
        reviews = ReviewQueue(tmp_path / "reviews.sqlite3", lease=timedelta(minutes=lease_minutes))
        pipeline = Pipeline(llm, calendar, CAL, BERLIN, llm_retry_delays=(0,), principal_path=f"/{OWNER}/",
                            checker=FakeChecker(confidence, checker_error), reviews=reviews)
        client = TestClient(create_app(s, pipeline=pipeline, llm=llm, calendar=calendar))
        client.__enter__()
        clients.append(client)
        return client

    yield build, clean_calendar
    for c in clients:
        c.__exit__(None, None, None)


def ingest(client, body=BODY):
    return client.post("/ingest?wait=30", json=body, headers=AUTH).json()


def stored(owner, uid):
    return owner.get(f"{CAL}{uid}.ics").text


# --- Marking --------------------------------------------------------------

def test_confident_event_is_added_normally(make):
    build, owner = make
    client = build(confidence=0.9)
    job = ingest(client)
    event = job["events"][0]
    assert job["ok"] and event["confidence"] == 0.9 and event["uncertain"] is False
    assert job["summary"] == "Added: Dentist, Tue 27 Oct 14:00–14:45"
    ics = stored(owner, event["uid"])
    assert "SUMMARY:Dentist" in ics and "TENTATIVE" not in ics
    assert client.get("/reviews/stats", headers=HELPER).json() == {}


def test_uncertain_event_is_added_marked_and_queued(make):
    build, owner = make
    client = build(confidence=0.2)
    job = ingest(client)
    event = job["events"][0]
    assert job["ok"] and event["uncertain"] is True and event["confidence"] == 0.2
    assert job["summary"] == "Added (uncertain, will be re-checked): Dentist, Tue 27 Oct 14:00–14:45"
    ics = stored(owner, event["uid"])
    assert "SUMMARY:(?) Dentist" in ics and "STATUS:TENTATIVE" in ics
    assert "X-INGEST-STATUS:UNCERTAIN" in ics and "X-INGEST-CONFIDENCE:0.20" in ics
    assert "Uncertain (confidence 0.20)" in ics
    assert client.get("/reviews/stats", headers=HELPER).json() == {"pending": 1}


def test_checker_down_means_uncertain(make):
    build, owner = make
    job = ingest(build(checker_error=True))
    assert job["events"][0]["uncertain"] is True and job["events"][0]["confidence"] == 0.0


def test_duplicates_and_conflicts_are_not_checked(make):
    build, owner = make
    client = build(confidence=0.9)
    ingest(client)
    checker = client.app.state.worker.pipeline.checker
    calls = len(checker.calls)
    assert ingest(client)["events"][0]["outcome"] == "duplicate"
    assert len(checker.calls) == calls


# --- Helper endpoints -----------------------------------------------------

def test_review_endpoints_need_the_helper_key(make):
    build, _ = make
    client = build()
    assert client.get("/reviews/next").status_code == 401
    assert client.get("/reviews/next", headers=AUTH).status_code == 401  # the ingest key is not enough
    assert build(helper=False).get("/reviews/next", headers=HELPER).status_code == 404


def test_next_hands_out_a_ready_task_once(make):
    build, _ = make
    client = build(confidence=0.2)
    ingest(client)
    task = client.get("/reviews/next", headers=HELPER).json()
    assert set(task) == {"id", "messages", "schema"}
    assert task["schema"]["required"] == ["event"]
    user = task["messages"][1]["content"]
    assert "Dentist Tuesday 14:00" in user and '"start": "2026-10-27T14:00"' in user
    assert "Day table:" in user
    assert client.get("/reviews/next", headers=HELPER).status_code == 204


def test_lease_expires(make):
    build, _ = make
    client = build(confidence=0.2, lease_minutes=0)
    ingest(client)
    first = client.get("/reviews/next", headers=HELPER).json()
    again = client.get("/reviews/next", headers=HELPER).json()
    assert first["id"] == again["id"]


def answer(client, task_id, **event):
    body = {"event": raw_event(**event)}
    return client.post(f"/reviews/{task_id}/result", json=body, headers=HELPER)


def test_helper_agrees_confirms_event(make):
    build, owner = make
    client = build(confidence=0.2)
    uid = ingest(client)["events"][0]["uid"]
    task = client.get("/reviews/next", headers=HELPER).json()
    resp = answer(client, task["id"])
    assert resp.json()["outcome"] == "confirmed"
    ics = stored(owner, uid)
    assert "SUMMARY:Dentist" in ics and "(?)" not in ics
    assert "STATUS:CONFIRMED" in ics and "X-INGEST-STATUS:CHECKED" in ics and "Checked by desktop model" in ics
    assert "DTSTART;TZID=Europe/Berlin:20261027T140000" in ics  # zone kept, not a fixed offset
    assert answer(client, task["id"]).status_code == 409  # already done
    assert client.get("/reviews/stats", headers=HELPER).json() == {"confirmed": 1}


def test_helper_correction_is_applied_in_place(make):
    build, owner = make
    client = build(confidence=0.2)
    uid = ingest(client)["events"][0]["uid"]
    task = client.get("/reviews/next", headers=HELPER).json()
    resp = answer(client, task["id"], start="2026-10-29T14:00", end="2026-10-29T14:45").json()
    assert resp == {"outcome": "corrected", "detail": "Tue 27 Oct 14:00–14:45 -> Thu 29 Oct 14:00–14:45"}
    ics = stored(owner, uid)
    assert "DTSTART;TZID=Europe/Berlin:20261029T140000" in ics
    assert "Corrected by desktop model (was Tue 27 Oct 14:00–14:45)" in ics and "(?)" not in ics


def test_correction_that_conflicts_is_not_applied(make):
    build, owner = make
    client = build(confidence=0.2)
    uid = ingest(client)["events"][0]["uid"]
    owner.put(f"{CAL}yoga.ics", headers={"Content-Type": "text/calendar"}, content=(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:t\r\nBEGIN:VEVENT\r\nUID:yoga\r\nDTSTAMP:20260101T000000Z\r\n"
        "DTSTART;TZID=Europe/Berlin:20261029T140000\r\nDTEND;TZID=Europe/Berlin:20261029T150000\r\n"
        "SUMMARY:Yoga\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n").encode())
    task = client.get("/reviews/next", headers=HELPER).json()
    resp = answer(client, task["id"], start="2026-10-29T14:00", end="2026-10-29T14:45").json()
    assert resp["outcome"] == "kept"
    ics = stored(owner, uid)
    assert "SUMMARY:(?) Dentist" in ics and "DTSTART;TZID=Europe/Berlin:20261027T140000" in ics
    description = str(icalendar.Calendar.from_ical(ics).walk("VEVENT")[0]["description"])
    assert "Desktop check suggests Thu 29 Oct 14:00–14:45; not applied because it conflicts with Yoga" in description


def test_event_changed_by_user_is_left_alone(make):
    build, owner = make
    client = build(confidence=0.2)
    uid = ingest(client)["events"][0]["uid"]
    ics = stored(owner, uid).replace("SUMMARY:(?) Dentist", "SUMMARY:Dentist (checked myself)")
    owner.put(f"{CAL}{uid}.ics", content=ics.encode(), headers={"Content-Type": "text/calendar"})
    task = client.get("/reviews/next", headers=HELPER).json()
    assert answer(client, task["id"], start="2026-10-29T14:00").json()["outcome"] == "skipped"
    assert "Dentist (checked myself)" in stored(owner, uid)


def test_deleted_event_is_skipped(make):
    build, owner = make
    client = build(confidence=0.2)
    uid = ingest(client)["events"][0]["uid"]
    owner.delete(f"{CAL}{uid}.ics")
    task = client.get("/reviews/next", headers=HELPER).json()
    assert answer(client, task["id"]).json() == {"outcome": "skipped", "detail": "the event was deleted"}


def test_invalid_answer_fails_item(make):
    build, owner = make
    client = build(confidence=0.2)
    uid = ingest(client)["events"][0]["uid"]
    task = client.get("/reviews/next", headers=HELPER).json()
    resp = answer(client, task["id"], start="2020-01-01T10:00", end=None).json()
    assert resp["outcome"] == "failed" and "past" in resp["detail"]
    assert "SUMMARY:(?) Dentist" in stored(owner, uid)
    bad = client.post(f"/reviews/{task['id']}/result", content=b"nope", headers=HELPER)
    assert bad.status_code == 409  # already finished


def test_unknown_review(make):
    build, _ = make
    assert build().post("/reviews/nope/result", json={}, headers=HELPER).status_code == 404


# --- Checker client against a stub of /v1/systemone -------------------------

class Stub:
    def __init__(self, reply):
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append(body)
                data = json.dumps(reply(body)).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


REF = datetime(2026, 11, 2, 18, 40, tzinfo=BERLIN)


def test_checker_request_and_probability():
    start = datetime(2026, 11, 5, 20, 30, tzinfo=BERLIN)

    def reply(body):
        options = list(body["questions"]["start"]["criteria"])
        probs = {o: (0.7 if o == "Thursday 05 November 2026, 20:30" else 0.1) for o in options}
        return {"answers": {"start": {"type": "choice", "choice": max(probs, key=probs.get),
                                      "probabilities": probs, "confidence": 0.5}}}

    stub = Stub(reply)
    assert Checker(stub.url).confidence("Kino Donnerstag 20:30", REF, "Kino", start) == 0.7
    question = stub.requests[0]["questions"]["start"]
    assert question["type"] == "choice" and len(question["criteria"]) == 4
    assert set(question["criteria"]) == {"Thursday 05 November 2026, 20:30", "Friday 06 November 2026, 20:30",
                                         "Wednesday 04 November 2026, 20:30", "Thursday 05 November 2026, 21:30"}
    assert "2026-11-05 Thursday" in stub.requests[0]["state"] and "Kino Donnerstag" in stub.requests[0]["state"]
    stub.server.shutdown()


def test_checker_options_are_shuffled_deterministically():
    start = datetime(2026, 11, 5, 20, 30, tzinfo=BERLIN)
    a, _ = build_request("t", REF, "Kino", start)
    b, _ = build_request("t", REF, "Kino", start)
    assert a == b
    orders = {tuple(build_request("t", REF, f"Event {i}", start)[0]["questions"]["start"]["criteria"])
              for i in range(10)}
    assert len(orders) > 1


def test_checker_all_day_options():
    from datetime import date
    body, extracted = build_request("t", REF, "Trip", date(2026, 11, 23))
    assert extracted == "Monday 23 November 2026"
    assert "Sunday 22 November 2026" in body["questions"]["start"]["criteria"]


def test_checker_errors():
    with pytest.raises(CheckerError):
        Checker("http://127.0.0.1:9", timeout=2).confidence("t", REF, "x", REF)
    stub = Stub(lambda body: {"answers": {"start": {"choice": "x"}}})
    with pytest.raises(CheckerError):
        Checker(stub.url).confidence("t", REF, "x", REF)
    stub.server.shutdown()
    assert not Checker("http://127.0.0.1:9").healthy()
