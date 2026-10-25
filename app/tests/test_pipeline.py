from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from calapp.common.caldav import CalDAVClient
from calapp.ingest.llm import LLMError
from calapp.ingest.models import IngestRequest
from calapp.ingest.pipeline import Pipeline
from conftest import OWNER, PASSWORDS, FakeLLM, raw_event

BERLIN = ZoneInfo("Europe/Berlin")
CALENDAR = f"/{OWNER}/personal/"
REQUEST = IngestRequest(text="Dentist Tuesday 14:00", received_at=datetime(2026, 10, 23, 9, 0, tzinfo=BERLIN))


@pytest.fixture
def ingest_client(radicale):
    client = CalDAVClient(radicale, "ingest", PASSWORDS["ingest"])
    yield client
    client.close()


def make(llm, client):
    return Pipeline(llm, client, CALENDAR, BERLIN, llm_retry_delays=(0, 0))


def stored(owner_http) -> list[str]:
    resp = owner_http.request("PROPFIND", CALENDAR, headers={"Depth": "1"})
    return [line for line in resp.text.split("<") if line.startswith("href>") and line.endswith(".ics")]


def test_adds_event_then_reports_duplicate(clean_calendar, ingest_client):
    pipeline = make(FakeLLM([raw_event()]), ingest_client)
    first = pipeline.run("job1", REQUEST)
    assert [r.outcome for r in first] == ["added"]
    assert len(stored(clean_calendar)) == 1

    ics = clean_calendar.get(f"{CALENDAR}{first[0].uid}.ics").text
    assert "SUMMARY:Dentist" in ics and "X-INGEST-JOB:job1" in ics and "CATEGORIES:llm-ingest" in ics

    second = pipeline.run("job2", REQUEST)
    assert [r.outcome for r in second] == ["duplicate"]
    assert len(stored(clean_calendar)) == 1


def test_same_event_twice_in_one_text(clean_calendar, ingest_client):
    results = make(FakeLLM([raw_event(), raw_event(title="dentist")]), ingest_client).run("j", REQUEST)
    assert [r.outcome for r in results] == ["added", "duplicate"]


def test_invalid_events_are_skipped_and_others_added(clean_calendar, ingest_client):
    llm = FakeLLM([raw_event(start="2026-01-01T10:00", end=None), raw_event(title="Lunch")])
    results = make(llm, ingest_client).run("j", REQUEST)
    assert [(r.outcome, r.reason) for r in results] == [("invalid", "in the past"), ("added", None)]


def test_text_is_cleaned_and_context_passed_to_llm(clean_calendar, ingest_client):
    llm = FakeLLM([raw_event()])
    request = IngestRequest(text="<p>Dentist&nbsp;Tuesday</p>", source="email", timezone="Europe/London",
                            received_at=datetime(2026, 10, 23, 9, 0))
    make(llm, ingest_client).run("j", request)
    call = llm.calls[0]
    assert call["text"] == "Dentist Tuesday"
    assert call["source"] == "email"
    assert call["timezone"] == "Europe/London"
    assert call["reference"] == datetime(2026, 10, 23, 9, 0, tzinfo=ZoneInfo("Europe/London"))


def test_llm_failure_is_raised(ingest_client):
    with pytest.raises(LLMError):
        make(FakeLLM(error="llm_bad_output"), ingest_client).run("j", REQUEST)


def test_llm_unreachable_is_retried(ingest_client, clean_calendar):
    class Flaky(FakeLLM):
        def extract(self, *args, **kwargs):
            if len(self.calls) < 2:
                self.calls.append({})
                raise LLMError("llm_unreachable")
            return super().extract(*args, **kwargs)

    results = make(Flaky([raw_event()]), ingest_client).run("j", REQUEST)
    assert [r.outcome for r in results] == ["added"]


def test_calendar_down_marks_events_as_error():
    client = CalDAVClient("http://127.0.0.1:9", "ingest", "x", timeout=1)
    results = make(FakeLLM([raw_event()]), client).run("j", REQUEST)
    assert [(r.outcome, r.reason) for r in results] == [("error", "calendar_unreachable")]


def test_ingest_account_cannot_overwrite_existing_event(clean_calendar, ingest_client):
    """create() must never replace an event, even one with the same UID."""
    assert ingest_client.create(CALENDAR, "fixed-uid", b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\n"
                                b"BEGIN:VEVENT\r\nUID:fixed-uid\r\nDTSTAMP:20260101T000000Z\r\n"
                                b"DTSTART:20261101T100000Z\r\nSUMMARY:Original\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    assert not ingest_client.create(CALENDAR, "fixed-uid", b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\n"
                                    b"BEGIN:VEVENT\r\nUID:fixed-uid\r\nDTSTAMP:20260101T000000Z\r\n"
                                    b"DTSTART:20261101T100000Z\r\nSUMMARY:Replaced\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    assert "SUMMARY:Original" in clean_calendar.get(f"{CALENDAR}fixed-uid.ics").text
