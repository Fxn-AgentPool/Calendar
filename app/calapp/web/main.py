"""Read-only agenda, week and month views of the calendar.

Run with: uvicorn calapp.web.main:create_app --factory
"""

from __future__ import annotations

import asyncio
import base64
import calendar as calmod
import logging
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from calapp.common.caldav import CalDAVClient, CalDAVError, CalendarInfo
from calapp.common.events import Occurrence, expand, safe_color
from calapp.common.settings import WebSettings
from calapp.web.hashpw import check_password

log = logging.getLogger("calapp.web")

HERE = Path(__file__).parent
MONTH_CELL_LIMIT = 3
# Fallback colours for calendars without one.
PALETTE = ["#2563eb", "#16a34a", "#d97706", "#9333ea", "#dc2626", "#0891b2"]

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; img-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "private, no-store",
}


class CalendarCache:
    """Short-lived cache so reloading a page doesn't query Radicale every time."""

    def __init__(self, client: CalDAVClient, principal: str, tz, ttl: float):
        self.client = client
        self.principal = principal
        self.tz = tz
        self.ttl = ttl
        self._lock = threading.Lock()
        self._entries: dict[tuple, tuple[float, object]] = {}

    def _cached(self, key: tuple, load: Callable[[], object]):
        now = time.monotonic()
        with self._lock:
            hit = self._entries.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        value = load()
        with self._lock:
            if len(self._entries) > 200:
                self._entries.clear()
            self._entries[key] = (now, value)
        return value

    def calendars(self) -> list[CalendarInfo]:
        return self._cached(("calendars",), lambda: self.client.list_calendars(self.principal))

    def occurrences(self, first: date, last: date) -> list[Occurrence]:
        """Occurrences touching the local days first..last (inclusive)."""

        def load():
            start = datetime.combine(first, datetime.min.time(), self.tz)
            end = datetime.combine(last + timedelta(days=1), datetime.min.time(), self.tz)
            result = []
            for index, cal in enumerate(self.calendars()):
                for occ in expand(self.client.query(cal.path, start, end), start, end, self.tz):
                    occ.calendar = cal.name
                    occ.calendar_index = index
                    result.append(occ)
            result.sort(key=lambda o: (o.range(self.tz)[0], not o.all_day, o.title.casefold()))
            return result

        return self._cached(("occ", first, last), load)


def by_day(occurrences: list[Occurrence], days: list[date], tz) -> dict[date, list[Occurrence]]:
    grouped: dict[date, list[Occurrence]] = {d: [] for d in days}
    for occ in occurrences:
        for d in occ.days(tz):
            if d in grouped:
                grouped[d].append(occ)
    for items in grouped.values():
        items.sort(key=lambda o: (not o.all_day, o.range(tz)[0], o.title.casefold()))
    return grouped


def create_app(
    settings: WebSettings | None = None,
    client: CalDAVClient | None = None,
    today: Callable[[], date] | None = None,
) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = settings or WebSettings.from_env()
    tz = settings.caldav.timezone
    caldav = settings.caldav
    client = client or CalDAVClient(caldav.url, caldav.user, caldav.password)
    cache = CalendarCache(client, caldav.principal_path, tz, settings.cache_seconds)
    today = today or (lambda: datetime.now(tz).date())

    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html"]))
    env.globals.update(tz=tz)

    def fmt_time(value: datetime | date) -> str:
        return value.astimezone(tz).strftime("%H:%M") if isinstance(value, datetime) else ""

    def fmt_span(occ: Occurrence, day: date | None = None) -> str:
        """Time label of an occurrence, as seen on ``day`` (None: full span)."""
        if occ.all_day:
            last = occ.end - timedelta(days=1)  # type: ignore[operator]
            if last == occ.start:
                return "all day"
            return f"{occ.start:%a %d %b} – {last:%a %d %b}"
        start, end = occ.range(tz)
        if day is None:
            if start.date() == end.date():
                return f"{start:%a %d %b %Y, %H:%M}–{end:%H:%M}"
            return f"{start:%a %d %b %H:%M} – {end:%a %d %b %H:%M}"
        begins = start.date() == day
        ends = end.date() == day or end == datetime.combine(day + timedelta(days=1), datetime.min.time(), tz)
        if begins and ends:
            return f"{start:%H:%M}–{end:%H:%M}"
        if begins:
            return f"{start:%H:%M} →"
        if ends:
            return f"→ {end:%H:%M}"
        return "all day"

    env.filters.update(time=fmt_time, span=fmt_span)

    app = FastAPI(title="agenda", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if settings.password_hash and request.url.path != "/healthz":
            if not _basic_auth_ok(request.headers.get("authorization", ""), caldav.owner, settings.password_hash):
                await asyncio.sleep(1)
                return PlainTextResponse(
                    "login required", status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="agenda"', **SECURITY_HEADERS},
                )
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    async def render(template: str, status: int = 200, **context) -> HTMLResponse:
        context.setdefault("today", today())
        html = env.get_template(template).render(**context)
        return HTMLResponse(html, status_code=status)

    async def load(first: date, last: date) -> list[Occurrence] | None:
        try:
            return await asyncio.to_thread(cache.occurrences, first, last)
        except CalDAVError as e:
            log.warning("calendar unreachable: %s", e)
            return None

    async def unavailable() -> HTMLResponse:
        return await render("error.html", status=503, title="Calendar unreachable",
                            message="The calendar server did not answer. Try again in a moment.")

    def parse_date(value: str, fmt: str) -> date:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            raise HTTPException(404) from None

    @app.get("/", response_class=HTMLResponse)
    async def agenda(start: str | None = None):
        first = parse_date(start, "%Y-%m-%d") if start else today()
        days = [first + timedelta(days=i) for i in range(settings.agenda_days)]
        occurrences = await load(days[0], days[-1])
        if occurrences is None:
            return await unavailable()
        grouped = by_day(occurrences, days, tz)
        step = timedelta(days=settings.agenda_days)
        return await render(
            "agenda.html", view="agenda", days=days, grouped=grouped,
            prev_url=f"/?start={first - step}", next_url=f"/?start={first + step}", today_url="/",
            title=f"{days[0]:%d %b} – {days[-1]:%d %b %Y}",
        )

    @app.get("/week/{day}", response_class=HTMLResponse)
    async def week(day: str):
        monday = parse_date(day, "%Y-%m-%d")
        monday -= timedelta(days=monday.weekday())
        days = [monday + timedelta(days=i) for i in range(7)]
        occurrences = await load(days[0], days[-1])
        if occurrences is None:
            return await unavailable()
        iso = monday.isocalendar()
        return await render(
            "week.html", view="week", days=days, grouped=by_day(occurrences, days, tz),
            prev_url=f"/week/{monday - timedelta(days=7)}", next_url=f"/week/{monday + timedelta(days=7)}",
            today_url=f"/week/{today()}", title=f"Week {iso.week}, {days[0]:%d %b} – {days[-1]:%d %b %Y}",
        )

    @app.get("/month/{month}", response_class=HTMLResponse)
    async def month(month: str):
        first = parse_date(month, "%Y-%m")
        weeks = calmod.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)
        days = [d for w in weeks for d in w]
        occurrences = await load(days[0], days[-1])
        if occurrences is None:
            return await unavailable()
        prev_month = (first - timedelta(days=1)).replace(day=1)
        next_month = (first + timedelta(days=32)).replace(day=1)
        return await render(
            "month.html", view="month", weeks=weeks, month=first, grouped=by_day(occurrences, days, tz),
            limit=MONTH_CELL_LIMIT, prev_url=f"/month/{prev_month:%Y-%m}", next_url=f"/month/{next_month:%Y-%m}",
            today_url=f"/month/{today():%Y-%m}", title=f"{first:%B %Y}",
        )

    @app.get("/event", response_class=HTMLResponse)
    async def event(day: str, uid: str):
        d = parse_date(day, "%Y-%m-%d")
        occurrences = await load(d - timedelta(days=1), d + timedelta(days=1))
        if occurrences is None:
            return await unavailable()
        match = next((o for o in occurrences if o.uid == uid and d in o.days(tz)), None)
        if match is None:
            return await render("error.html", status=404, title="Event not found",
                                message="It may have been moved or deleted.")
        return await render("event.html", view="event", occ=match, day=d, title=match.title,
                            back_url=f"/?start={d}")

    @app.get("/calendars.css")
    async def calendar_colors():
        try:
            calendars = await asyncio.to_thread(cache.calendars)
        except CalDAVError:
            calendars = []
        rules = []
        for index in range(max(len(calendars), len(PALETTE))):
            color = safe_color(calendars[index].color) if index < len(calendars) else None
            rules.append(f".cal-{index} {{ --cal: {color or PALETTE[index % len(PALETTE)]}; }}")
        return Response("\n".join(rules) + "\n", media_type="text/css")

    @app.get("/healthz")
    async def healthz():
        ok = await asyncio.to_thread(client.ping, caldav.principal_path)
        return Response('{"status": "%s"}' % ("ok" if ok else "degraded"),
                        status_code=200 if ok else 503, media_type="application/json")

    return app


def _basic_auth_ok(header: str, user: str, password_hash: str) -> bool:
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        given_user, _, password = base64.b64decode(encoded).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return given_user == user and check_password(password, password_hash)
