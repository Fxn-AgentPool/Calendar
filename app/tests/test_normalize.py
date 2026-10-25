from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import icalendar
import pytest

from calapp.ingest.pipeline import Invalid, describe, normalize, to_ical
from conftest import raw_event

BERLIN = ZoneInfo("Europe/Berlin")
REFERENCE = datetime(2026, 10, 23, 9, 0, tzinfo=BERLIN)


def norm(raw, reference=REFERENCE):
    return normalize(raw, reference=reference, user_tz=BERLIN, default_minutes=60)


def test_timed_event_with_end():
    event = norm(raw_event())
    assert event.start == datetime(2026, 10, 27, 14, 0, tzinfo=BERLIN)
    assert event.end == datetime(2026, 10, 27, 14, 45, tzinfo=BERLIN)
    assert not event.all_day


def test_missing_end_uses_default_duration():
    event = norm(raw_event(end=None))
    assert event.end - event.start == timedelta(minutes=60)


def test_overnight_event_ends_next_day():
    event = norm(raw_event(start="2026-10-24T22:00", end="2026-10-24T01:00"))
    assert event.end == datetime(2026, 10, 25, 1, 0, tzinfo=BERLIN)


def test_end_on_earlier_day_is_invalid():
    with pytest.raises(Invalid, match="ends before"):
        norm(raw_event(start="2026-10-27T14:00", end="2026-10-26T15:00"))


def test_longer_than_a_day_is_invalid():
    with pytest.raises(Invalid, match="24 hours"):
        norm(raw_event(start="2026-10-27T14:00", end="2026-10-28T15:00"))


def test_impossible_date_is_invalid():
    with pytest.raises(Invalid, match="impossible"):
        norm(raw_event(start="2026-15-40T14:00", end=None))


def test_past_and_far_future_are_invalid():
    with pytest.raises(Invalid, match="past"):
        norm(raw_event(start="2026-10-01T10:00", end=None))
    with pytest.raises(Invalid, match="two years"):
        norm(raw_event(start="2029-01-01T10:00", end=None))


def test_earlier_today_is_allowed():
    event = norm(raw_event(start="2026-10-23T07:00", end=None))
    assert event.start.hour == 7


def test_all_day_single_and_range():
    single = norm(raw_event(title="Day off", all_day=True, start="2026-10-30", end=None))
    assert (single.start, single.end, single.all_day) == (date(2026, 10, 30), date(2026, 10, 31), True)
    trip = norm(raw_event(title="Vienna", all_day=True, start="2026-11-23", end="2026-11-27"))
    assert (trip.start, trip.end) == (date(2026, 11, 23), date(2026, 11, 28))


def test_date_without_time_becomes_all_day():
    event = norm(raw_event(start="2026-10-30", end=None))
    assert event.all_day and event.start == date(2026, 10, 30)


def test_other_timezone_is_converted():
    event = norm(raw_event(start="2026-11-02T09:00", end="2026-11-02T09:30", timezone="America/New_York"))
    assert event.start.astimezone(BERLIN).hour == 15


def test_unknown_timezone_falls_back_to_user_zone():
    event = norm(raw_event(timezone="Mars/Olympus"))
    assert event.start.tzinfo == BERLIN


def test_dst_change_day():
    # 2026-10-25 is the end of summer time in Berlin: 01:30 to 04:30 is 4 real hours.
    event = norm(raw_event(start="2026-10-25T01:30", end="2026-10-25T04:30"))
    assert event.end - event.start == timedelta(hours=3)  # wall-clock difference
    assert (event.end.timestamp() - event.start.timestamp()) / 3600 == 4


def test_uid_is_stable_and_ignores_case_and_spacing():
    a = norm(raw_event(title="Dentist  appointment"))
    b = norm(raw_event(title="dentist appointment"))
    c = norm(raw_event(title="Dentist appointment", start="2026-10-27T15:00", end=None))
    assert a.uid == b.uid != c.uid


def test_text_fields_are_trimmed():
    event = norm(raw_event(title="  Dentist \n", location="x" * 500, notes="  bring card  "))
    assert event.title == "Dentist"
    assert len(event.location) == 200
    assert event.notes == "bring card"


def test_ical_output():
    event = norm(raw_event(location="Hauptstr. 5"))
    cal = icalendar.Calendar.from_ical(to_ical(event, "job123"))
    vevent = cal.walk("VEVENT")[0]
    assert str(vevent["uid"]) == event.uid
    assert vevent.decoded("dtstart") == event.start
    assert vevent["x-ingest-job"] == "job123"
    assert "llm-ingest" in vevent["categories"].to_ical().decode()
    assert cal.walk("VTIMEZONE")[0]["tzid"] == "Europe/Berlin"


def test_describe():
    event = norm(raw_event())
    assert describe(event.start, event.end, False, BERLIN) == "Tue 27 Oct 14:00–14:45"
    trip = norm(raw_event(all_day=True, start="2026-11-23", end="2026-11-27"))
    assert describe(trip.start, trip.end, True, BERLIN) == "Mon 23 Nov – Fri 27 Nov"
