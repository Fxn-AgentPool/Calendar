from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from calapp.common.caldav import CalDAVClient
from calapp.ingest.main import create_app
from calapp.ingest.models import IngestRequest
from calapp.ingest.pipeline import Pipeline
from conftest import OWNER, PASSWORDS, FakeLLM, raw_event
from test_api import AUTH, settings

BERLIN = ZoneInfo("Europe/Berlin")
REQUEST = IngestRequest(text="x", received_at=datetime(2026, 10, 23, 9, 0, tzinfo=BERLIN))


def put(owner_http, calendar: str, uid: str, lines: str) -> None:
    body = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTAMP:20260101T000000Z\r\n{lines}END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    resp = owner_http.put(f"/{OWNER}/{calendar}/{uid}.ics", content=body.encode(),
                          headers={"Content-Type": "text/calendar"})
    assert resp.status_code == 201, resp.text


@pytest.fixture
def owner(clean_calendar):
    clean_calendar.request(
        "MKCOL", f"/{OWNER}/work/",
        content=b'<?xml version="1.0"?><d:mkcol xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        b"<d:set><d:prop><d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
        b"<d:displayname>Work</d:displayname></d:prop></d:set></d:mkcol>",
    )
    put(clean_calendar, "personal", "yoga",
        "DTSTART;TZID=Europe/Berlin:20261027T133000\r\nDTEND;TZID=Europe/Berlin:20261027T143000\r\nSUMMARY:Yoga\r\n")
    put(clean_calendar, "work", "standup",
        "DTSTART;TZID=Europe/Berlin:20261020T090000\r\nDTEND;TZID=Europe/Berlin:20261020T093000\r\n"
        "RRULE:FREQ=WEEKLY;BYDAY=TU\r\nSUMMARY:Standup\r\n")
    put(clean_calendar, "personal", "free",
        "DTSTART;TZID=Europe/Berlin:20261028T100000\r\nDTEND;TZID=Europe/Berlin:20261028T120000\r\n"
        "TRANSP:TRANSPARENT\r\nSUMMARY:Maybe gym\r\n")
    put(clean_calendar, "personal", "holiday",
        "DTSTART;VALUE=DATE:20261029\r\nDTEND;VALUE=DATE:20261030\r\nSUMMARY:Holiday\r\n")
    put(clean_calendar, "personal", "cancelled",
        "DTSTART;TZID=Europe/Berlin:20261030T100000\r\nDTEND;TZID=Europe/Berlin:20261030T110000\r\n"
        "STATUS:CANCELLED\r\nSUMMARY:Cancelled meeting\r\n")
    return clean_calendar


@pytest.fixture
def run(radicale, owner):
    client = CalDAVClient(radicale, "ingest", PASSWORDS["ingest"])

    def run(*events, all_day_conflicts=False):
        pipeline = Pipeline(FakeLLM(list(events)), client, f"/{OWNER}/personal/", BERLIN,
                            llm_retry_delays=(0,), principal_path=f"/{OWNER}/",
                            all_day_conflicts=all_day_conflicts)
        return pipeline.run("job", REQUEST)

    yield run
    client.close()


def outcome(results):
    return [(r.outcome, r.conflicts_with) for r in results]


def test_overlap_in_same_calendar(run):
    assert outcome(run(raw_event(start="2026-10-27T14:00", end="2026-10-27T14:45"))) == [("conflict", ["Yoga"])]


def test_overlap_with_repeating_event_in_other_calendar(run):
    results = run(raw_event(title="Call", start="2026-11-03T09:15", end="2026-11-03T09:45"))
    assert outcome(results) == [("conflict", ["Standup"])]


def test_back_to_back_is_fine(run):
    assert outcome(run(raw_event(start="2026-10-27T14:30", end="2026-10-27T15:00"))) == [("added", None)]
    assert outcome(run(raw_event(title="Late", start="2026-10-27T13:00", end="2026-10-27T13:30"))) == [("added", None)]


def test_free_and_cancelled_events_do_not_block(run):
    assert outcome(run(raw_event(start="2026-10-28T10:30", end=None))) == [("added", None)]
    assert outcome(run(raw_event(start="2026-10-30T10:00", end=None))) == [("added", None)]


def test_all_day_events_only_count_when_enabled(run):
    timed_on_holiday = raw_event(start="2026-10-29T10:00", end=None)
    assert outcome(run(timed_on_holiday)) == [("added", None)]
    day_off = raw_event(title="Day off", all_day=True, start="2026-10-27", end=None)
    assert outcome(run(day_off)) == [("added", None)]


def test_all_day_conflicts_setting(run):
    results = run(raw_event(start="2026-10-29T10:00", end=None), all_day_conflicts=True)
    assert outcome(results) == [("conflict", ["Holiday"])]


def test_duplicate_wins_over_conflict(run):
    event = raw_event(start="2026-11-05T10:00", end=None)
    assert outcome(run(event)) == [("added", None)]
    assert outcome(run(event)) == [("duplicate", None)]


def test_events_from_the_same_text_can_conflict(run):
    results = run(raw_event(title="A", start="2026-11-05T10:00", end=None),
                  raw_event(title="B", start="2026-11-05T10:30", end=None))
    assert outcome(results) == [("added", None), ("conflict", ["A"])]


def test_conflict_is_reported_in_summary(radicale, owner, tmp_path):
    s = settings(radicale, tmp_path)
    llm = FakeLLM([raw_event(start="2026-10-27T14:00", end="2026-10-27T14:45")])
    with TestClient(create_app(s, llm=llm)) as client:
        job = client.post("/ingest?wait=30", json={"text": "x", "received_at": "2026-10-23T09:00:00+02:00"},
                          headers=AUTH).json()
    assert job["ok"] is False
    assert job["summary"] == "Not added (conflicts with Yoga): Dentist, Tue 27 Oct 14:00–14:45"
    assert job["events"][0]["conflicts_with"] == ["Yoga"]
