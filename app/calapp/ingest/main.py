"""Ingest API: accepts text, extracts events in the background, reports results.

Run with: uvicorn calapp.ingest.main:create_app --factory
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import hmac
import logging
import time
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from calapp.common.caldav import CalDAVClient
from calapp.common.settings import IngestSettings
from calapp.ingest.jobs import JobStore
from calapp.ingest.llm import LLMClient, LLMError
from calapp.ingest.models import IngestRequest, JobResult
from calapp.ingest.pipeline import Pipeline, summarize

log = logging.getLogger("calapp.ingest")

MAX_WAIT_SECONDS = 90


class RateLimiter:
    """Sliding one-hour window over all requests (there is one API key)."""

    def __init__(self, per_hour: int):
        self.per_hour = per_hour
        self._hits: collections.deque[float] = collections.deque()

    def check(self) -> int | None:
        """Returns None if allowed, else seconds until the next request is allowed."""
        now = time.monotonic()
        while self._hits and now - self._hits[0] > 3600:
            self._hits.popleft()
        if len(self._hits) >= self.per_hour:
            return int(3600 - (now - self._hits[0])) + 1
        self._hits.append(now)
        return None


class Worker:
    """Processes jobs one at a time; the LLM serves one request at a time anyway."""

    def __init__(self, store: JobStore, pipeline: Pipeline, user_tz):
        self.store = store
        self.pipeline = pipeline
        self.user_tz = user_tz
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._done: dict[str, asyncio.Event] = {}

    def submit(self, job_id: str) -> None:
        self._done[job_id] = asyncio.Event()
        self.queue.put_nowait(job_id)

    async def wait(self, job_id: str, seconds: float) -> None:
        event = self._done.get(job_id)
        if event is not None:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(event.wait(), seconds)

    def _process(self, job_id: str) -> None:
        request = self.store.input(job_id)
        if request is None:
            return
        self.store.mark_running(job_id)
        started = time.monotonic()
        digest = hashlib.sha256(request.text.encode()).hexdigest()[:12]
        try:
            events = self.pipeline.run(job_id, request)
        except LLMError as e:
            log.warning("job %s input=%s failed: %s", job_id, digest, e)
            self.store.fail(job_id, e.code)
            return
        except Exception:
            log.exception("job %s input=%s crashed", job_id, digest)
            self.store.fail(job_id, "internal_error")
            return
        counts = collections.Counter(e.outcome for e in events)
        log.info(
            "job %s input=%s chars=%d took=%.1fs %s",
            job_id, digest, len(request.text), time.monotonic() - started,
            " ".join(f"{k}={v}" for k, v in sorted(counts.items())),
        )
        self.store.finish(job_id, events, summarize(events, self.user_tz))

    async def run(self) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                await asyncio.to_thread(self._process, job_id)
            finally:
                event = self._done.pop(job_id, None)
                if event is not None:
                    event.set()


def create_app(
    settings: IngestSettings | None = None,
    pipeline: Pipeline | None = None,
    llm: LLMClient | None = None,
    calendar: CalDAVClient | None = None,
) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = settings or IngestSettings.from_env()
    llm = llm or LLMClient(settings.llm_url, timeout=settings.llm_timeout)
    caldav = settings.caldav
    calendar = calendar or CalDAVClient(caldav.url, caldav.user, caldav.password)
    pipeline = pipeline or Pipeline(
        llm, calendar, settings.calendar_path, caldav.timezone,
        default_minutes=settings.default_event_minutes, max_text_chars=settings.max_text_chars,
    )
    store = JobStore(Path(settings.data_dir) / "jobs.sqlite3")
    limiter = RateLimiter(settings.rate_limit_per_hour)
    expected_hash = bytes.fromhex(settings.api_key_hash)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        worker = Worker(store, pipeline, caldav.timezone)
        app.state.worker = worker
        for job_id in store.unfinished():
            worker.submit(job_id)

        async def expire_loop():
            while True:
                removed = store.expire(timedelta(hours=settings.job_ttl_hours))
                if removed:
                    log.info("expired %d old jobs", removed)
                await asyncio.sleep(600)

        tasks = [asyncio.create_task(worker.run()), asyncio.create_task(expire_loop())]
        yield
        for task in tasks:
            task.cancel()

    app = FastAPI(title="calendar ingest", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    async def require_key(request: Request) -> None:
        header = request.headers.get("authorization", "")
        scheme, _, key = header.partition(" ")
        given = hashlib.sha256(key.strip().encode()).digest()
        if scheme.lower() != "bearer" or not hmac.compare_digest(given, expected_hash):
            await asyncio.sleep(1)  # slows down guessing
            raise HTTPException(401, "invalid or missing API key", headers={"WWW-Authenticate": "Bearer"})

    @app.post("/ingest", dependencies=[Depends(require_key)])
    async def ingest(request: Request, wait: int = Query(0, ge=0, le=MAX_WAIT_SECONDS)):
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_body_bytes:
            raise HTTPException(413, "request too large")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > settings.max_body_bytes:
                raise HTTPException(413, "request too large")
        try:
            payload = IngestRequest.model_validate_json(bytes(body))
        except ValidationError as e:
            errors = [{"field": ".".join(map(str, err["loc"])), "message": err["msg"]} for err in e.errors()]
            return JSONResponse({"detail": errors}, status_code=422)

        retry_after = limiter.check()
        if retry_after is not None:
            raise HTTPException(429, "rate limit reached", headers={"Retry-After": str(retry_after)})

        job_id = store.create(payload)
        worker: Worker = request.app.state.worker
        worker.submit(job_id)
        if wait:
            await worker.wait(job_id, wait)
        job = store.get(job_id)
        assert job is not None
        if job.status in ("done", "failed"):
            return JSONResponse(job.model_dump(mode="json"), status_code=200)
        return JSONResponse(
            {"job_id": job.job_id, "status": job.status},
            status_code=202,
            headers={"Location": f"/jobs/{job_id}"},
        )

    @app.get("/jobs/{job_id}", dependencies=[Depends(require_key)], response_model=JobResult)
    async def get_job(job_id: str):
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, "unknown or expired job")
        return job

    @app.get("/healthz")
    async def healthz():
        llm_ok, cal_ok = await asyncio.gather(
            asyncio.to_thread(llm.healthy),
            asyncio.to_thread(calendar.ping, settings.calendar_path),
        )
        ok = llm_ok and cal_ok
        return JSONResponse({"status": "ok" if ok else "degraded"}, status_code=200 if ok else 503)

    return app
