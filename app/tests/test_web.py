from datetime import date
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from calapp.common.caldav import CalDAVClient
from calapp.common.settings import CalDAVSettings, WebSettings
from calapp.web.hashpw import hash_password
from calapp.web.main import create_app
from conftest import OWNER, PASSWORDS

TODAY = date(2026, 10, 26)  # a Monday


def ics(uid: str, lines: str) -> bytes:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTAMP:20260101T000000Z\r\n{lines}END:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


EVENTS = {
    "dentist": "DTSTART;TZID=Europe/Berlin:20261027T140000\r\nDTEND;TZID=Europe/Berlin:20261027T144500\r\n"
               "SUMMARY:Dentist\r\nLOCATION:Hauptstr. 5\r\nX-INGEST-JOB:job42\r\nCATEGORIES:llm-ingest\r\n",
    "standup": "DTSTART;TZID=Europe/Berlin:20261020T090000\r\nDTEND;TZID=Europe/Berlin:20261020T091500\r\n"
               "RRULE:FREQ=WEEKLY;BYDAY=MO,WE\r\nSUMMARY:Standup\r\n",
    "trip": "DTSTART;VALUE=DATE:20261029\r\nDTEND;VALUE=DATE:20261101\r\nSUMMARY:Vienna trip\r\n",
    "unsure": "DTSTART;TZID=Europe/Berlin:20261103T100000\r\nDTEND;TZID=Europe/Berlin:20261103T110000\r\n"
              "SUMMARY:(?) Call Anna\r\nSTATUS:TENTATIVE\r\nX-INGEST-STATUS:UNCERTAIN\r\nX-INGEST-JOB:job7\r\n",
    "night": "DTSTART:20261030T210000Z\r\nDTEND:20261031T010000Z\r\nSUMMARY:Night shift\r\n",
    "evil": "DTSTART:20261028T100000Z\r\nDTEND:20261028T110000Z\r\n"
            "SUMMARY:<script>alert(1)</script>\r\nDESCRIPTION:<img src=x onerror=alert(1)>\r\n",
    "cancelled": "DTSTART:20261028T120000Z\r\nDTEND:20261028T130000Z\r\nSUMMARY:Cancelled thing\r\n"
                 "STATUS:CANCELLED\r\n",
}


@pytest.fixture
def owner_calendar(clean_calendar):
    for uid, lines in EVENTS.items():
        resp = clean_calendar.put(f"/{OWNER}/personal/{uid}.ics", content=ics(uid, lines),
                                  headers={"Content-Type": "text/calendar"})
        assert resp.status_code == 201, resp.text
    return clean_calendar


def make_settings(radicale, **overrides) -> WebSettings:
    values = dict(
        caldav=CalDAVSettings(radicale, "agenda", PASSWORDS["agenda"], OWNER, ZoneInfo("Europe/Berlin")),
        agenda_days=14, cache_seconds=0, password_hash=None,
    )
    values.update(overrides)
    return WebSettings(**values)


@pytest.fixture
def web(radicale, owner_calendar):
    settings = make_settings(radicale)
    client = CalDAVClient(radicale, "agenda", PASSWORDS["agenda"])
    return TestClient(create_app(settings, client=client, today=lambda: TODAY))


def test_agenda_lists_events_by_day(web):
    page = web.get("/").text
    assert "Dentist" in page and "14:00–14:45" in page and "Hauptstr. 5" in page
    # Standup repeats Mondays and Wednesdays.
    assert page.count("Standup") == 4
    # The trip spans three days and appears on each.
    assert page.count("Vienna trip") == 3
    # Night shift crosses midnight (22:00–02:00 Berlin time).
    assert "22:00 →" in page and "→ 02:00" in page
    assert "Cancelled thing" not in page
    assert '<span class="badge" title="Added from text">auto</span>' in page


def test_event_text_is_escaped(web):
    page = web.get("/").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    detail = web.get("/event", params={"day": "2026-10-28", "uid": "evil"}).text
    assert "<img src=x" not in detail and "&lt;img src=x onerror=alert(1)&gt;" in detail


def test_security_headers(web):
    resp = web.get("/")
    csp = resp.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script" not in csp
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["cache-control"] == "private, no-store"


def test_week_view(web):
    page = web.get("/week/2026-10-28").text  # any day in the week works
    assert "Week 44" in page and "Mon <span>26 Oct</span>" in page
    assert page.count("Standup") == 2
    assert 'href="/week/2026-10-19"' in page and 'href="/week/2026-11-02"' in page


def test_month_view(web):
    page = web.get("/month/2026-10").text
    assert "October 2026" in page
    assert 'href="/month/2026-09"' in page and 'href="/month/2026-11"' in page
    # 20 Oct (the first occurrence), then every Mon and Wed: 21, 26, 28.
    assert page.count("Standup") == 4


def test_month_cell_limit(web, owner_calendar):
    for i in range(5):
        owner_calendar.put(f"/{OWNER}/personal/busy{i}.ics", headers={"Content-Type": "text/calendar"},
                           content=ics(f"busy{i}", f"DTSTART:20261023T0{i}0000Z\r\nDTEND:20261023T0{i}3000Z\r\n"
                                       "SUMMARY:Busy\r\n"))
    assert "+2 more" in web.get("/month/2026-10").text


def test_event_detail(web):
    page = web.get("/event", params={"day": "2026-10-27", "uid": "dentist"}).text
    assert "Dentist" in page and "Tue 27 Oct 2026, 14:00–14:45" in page
    assert "job42" in page and "Personal" in page
    assert web.get("/event", params={"day": "2026-10-27", "uid": "nope"}).status_code == 404


def test_bad_dates_are_404(web):
    assert web.get("/week/2026-13-01").status_code == 404
    assert web.get("/month/nonsense").status_code == 404
    assert web.get("/", params={"start": "x"}).status_code == 404


def test_read_only(web):
    for method in ("post", "put", "delete"):
        assert getattr(web, method)("/").status_code == 405


def test_calendar_colors_css(web, owner_calendar):
    owner_calendar.request(
        "PROPPATCH", f"/{OWNER}/personal/",
        content=b'<?xml version="1.0"?><d:propertyupdate xmlns:d="DAV:" xmlns:a="http://apple.com/ns/ical/">'
                b"<d:set><d:prop><a:calendar-color>#ff0000ff</a:calendar-color></d:prop></d:set></d:propertyupdate>",
    )
    css = web.get("/calendars.css")
    assert css.headers["content-type"].startswith("text/css")
    assert ".cal-0 { --cal: #ff0000; }" in css.text


def test_calendar_unreachable(radicale):
    settings = make_settings("http://127.0.0.1:9")
    client = CalDAVClient("http://127.0.0.1:9", "agenda", "x", timeout=1)
    app = TestClient(create_app(settings, client=client, today=lambda: TODAY))
    resp = app.get("/")
    assert resp.status_code == 503 and "Calendar unreachable" in resp.text
    assert app.get("/healthz").status_code == 503


def test_optional_password(radicale, owner_calendar):
    settings = make_settings(radicale, password_hash=hash_password("correct horse battery"))
    client = CalDAVClient(radicale, "agenda", PASSWORDS["agenda"])
    app = TestClient(create_app(settings, client=client, today=lambda: TODAY))
    assert app.get("/").status_code == 401
    assert app.get("/", auth=(OWNER, "wrong")).status_code == 401
    assert app.get("/", auth=(OWNER, "correct horse battery")).status_code == 200
    assert app.get("/healthz").status_code == 200


def test_cache_avoids_repeated_queries(radicale, owner_calendar):
    settings = make_settings(radicale, cache_seconds=60)

    class Counting(CalDAVClient):
        queries = 0

        def query(self, *args, **kwargs):
            Counting.queries += 1
            return super().query(*args, **kwargs)

    app = TestClient(create_app(settings, client=Counting(radicale, "agenda", PASSWORDS["agenda"]),
                                today=lambda: TODAY))
    app.get("/")
    app.get("/")
    assert Counting.queries == 1


def test_uncertain_events_have_a_badge(web):
    page = web.get("/").text
    assert 'class="badge uncertain"' in page and "(?) Call Anna" in page
    detail = web.get("/event", params={"day": "2026-11-03", "uid": "unsure"}).text
    assert "queued for a re-check" in detail
