"""Minimal CalDAV client for Radicale: just the requests this project needs."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import unquote

import httpx
import icalendar

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav", "a": "http://apple.com/ns/ical/"}


class CalDAVError(Exception):
    pass


@dataclass
class CalendarInfo:
    path: str
    name: str
    color: str | None


@dataclass
class CalendarObject:
    path: str
    calendar: icalendar.Calendar


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class CalDAVClient:
    def __init__(self, base_url: str, user: str, password: str, timeout: float = 15.0):
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=(user, password),
            timeout=timeout,
            headers={"User-Agent": "calapp"},
        )

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return self._http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise CalDAVError(f"calendar unreachable: {e}") from e

    def ping(self, path: str) -> bool:
        try:
            resp = self._request("PROPFIND", path, headers={"Depth": "0"})
        except CalDAVError:
            return False
        return resp.status_code == 207

    def create(self, calendar_path: str, uid: str, ical: bytes) -> bool:
        """Store a new object. Returns False if one with this UID already exists.

        ``If-None-Match: *`` makes the server refuse to overwrite, so this can
        never change an existing event.
        """
        path = f"{calendar_path.rstrip('/')}/{uid}.ics"
        resp = self._request(
            "PUT",
            path,
            content=ical,
            headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
        )
        if resp.status_code in (200, 201, 204):
            return True
        if resp.status_code == 412:
            return False
        raise CalDAVError(f"PUT {path} failed: HTTP {resp.status_code}")

    def get(self, path: str) -> tuple[icalendar.Calendar, str] | None:
        """One stored object and its ETag, or None if it doesn't exist."""
        resp = self._request("GET", path)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise CalDAVError(f"GET {path} failed: HTTP {resp.status_code}")
        return icalendar.Calendar.from_ical(resp.content), resp.headers.get("etag", "")

    def update(self, path: str, ical: bytes, etag: str) -> bool:
        """Replace an object only if it still has ``etag``. False if it changed meanwhile."""
        resp = self._request(
            "PUT",
            path,
            content=ical,
            headers={"Content-Type": "text/calendar; charset=utf-8", "If-Match": etag},
        )
        if resp.status_code in (200, 201, 204):
            return True
        if resp.status_code == 412:
            return False
        raise CalDAVError(f"PUT {path} failed: HTTP {resp.status_code}")

    def query(self, calendar_path: str, start: datetime, end: datetime) -> list[CalendarObject]:
        """Objects with a VEVENT overlapping [start, end) (recurring ones included)."""
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-data/></d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="VEVENT">
        <c:time-range start="{_utc(start)}" end="{_utc(end)}"/>
      </c:comp-filter>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>"""
        resp = self._request(
            "REPORT",
            calendar_path,
            content=body.encode(),
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        )
        if resp.status_code != 207:
            raise CalDAVError(f"REPORT {calendar_path} failed: HTTP {resp.status_code}")
        objects = []
        for response in ET.fromstring(resp.content).findall("d:response", NS):
            href = response.findtext("d:href", namespaces=NS)
            data = response.findtext("d:propstat/d:prop/c:calendar-data", namespaces=NS)
            if not href or not data:
                continue
            try:
                cal = icalendar.Calendar.from_ical(data)
            except ValueError:
                continue
            objects.append(CalendarObject(path=unquote(href), calendar=cal))
        return objects

    def list_calendars(self, principal_path: str) -> list[CalendarInfo]:
        body = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:a="http://apple.com/ns/ical/">
  <d:prop><d:resourcetype/><d:displayname/><a:calendar-color/></d:prop>
</d:propfind>"""
        resp = self._request(
            "PROPFIND",
            principal_path,
            content=body.encode(),
            headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        )
        if resp.status_code != 207:
            raise CalDAVError(f"PROPFIND {principal_path} failed: HTTP {resp.status_code}")
        calendars = []
        for response in ET.fromstring(resp.content).findall("d:response", NS):
            href = unquote(response.findtext("d:href", namespaces=NS) or "")
            prop = response.find("d:propstat/d:prop", NS)
            if prop is None or prop.find("d:resourcetype/c:calendar", NS) is None:
                continue
            name = prop.findtext("d:displayname", namespaces=NS) or href.rstrip("/").rsplit("/", 1)[-1]
            color = (prop.findtext("a:calendar-color", namespaces=NS) or "").strip() or None
            calendars.append(CalendarInfo(path=href, name=name, color=color))
        return calendars
