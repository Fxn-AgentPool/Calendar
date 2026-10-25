import hashlib
import time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from calapp.common.caldav import CalDAVClient
from calapp.common.settings import CalDAVSettings, IngestSettings
from calapp.ingest.jobs import JobStore
from calapp.ingest.main import create_app
from calapp.ingest.models import IngestRequest
from calapp.ingest.pipeline import Pipeline
from conftest import OWNER, PASSWORDS, FakeLLM, raw_event

KEY = "test-api-key-0123456789abcdef0123456789"
AUTH = {"Authorization": f"Bearer {KEY}"}
BODY = {"text": "Dentist Tuesday 14:00", "received_at": "2026-10-23T09:00:00+02:00"}


def settings(radicale, tmp_path, **overrides) -> IngestSettings:
    values = dict(
        caldav=CalDAVSettings(radicale, "ingest", PASSWORDS["ingest"], OWNER, ZoneInfo("Europe/Berlin")),
        calendar="personal",
        llm_url="http://unused",
        api_key_hash=hashlib.sha256(KEY.encode()).hexdigest(),
        default_event_minutes=60,
        rate_limit_per_hour=100,
        data_dir=str(tmp_path),
        job_ttl_hours=24,
        max_body_bytes=4096,
        max_text_chars=20_000,
        llm_timeout=10,
    )
    values.update(overrides)
    return IngestSettings(**values)


@pytest.fixture
def make_client(radicale, tmp_path, clean_calendar):
    clients = []

    def make(llm=None, **overrides):
        s = settings(radicale, tmp_path, **overrides)
        llm = llm or FakeLLM([raw_event()])
        calendar = CalDAVClient(radicale, "ingest", PASSWORDS["ingest"])
        pipeline = Pipeline(llm, calendar, s.calendar_path, s.caldav.timezone, llm_retry_delays=(0,))
        client = TestClient(create_app(s, pipeline=pipeline, llm=llm, calendar=calendar))
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def test_requires_api_key(make_client):
    client = make_client()
    assert client.post("/ingest", json=BODY).status_code == 401
    assert client.post("/ingest", json=BODY, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/jobs/x").status_code == 401


def test_wait_returns_result(make_client):
    resp = make_client().post("/ingest?wait=30", json=BODY, headers=AUTH)
    assert resp.status_code == 200
    job = resp.json()
    assert job["status"] == "done" and job["ok"] is True
    assert job["summary"] == "Added: Dentist, Tue 27 Oct 14:00–14:45"
    assert job["events"][0]["outcome"] == "added"
    assert job["events"][0]["start"] == "2026-10-27T14:00:00+01:00"


def test_without_wait_returns_job_to_poll(make_client):
    client = make_client(FakeLLM([raw_event()], delay=0.3))
    resp = client.post("/ingest", json=BODY, headers=AUTH)
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert resp.headers["location"] == f"/jobs/{job_id}"
    for _ in range(50):
        job = client.get(f"/jobs/{job_id}", headers=AUTH).json()
        if job["status"] == "done":
            break
        time.sleep(0.1)
    assert job["status"] == "done" and job["ok"]


def test_duplicate_is_ok_but_reported(make_client):
    client = make_client()
    client.post("/ingest?wait=30", json=BODY, headers=AUTH)
    job = client.post("/ingest?wait=30", json=BODY, headers=AUTH).json()
    assert job["ok"] is True
    assert job["summary"].startswith("Already in calendar: Dentist")


def test_llm_failure(make_client):
    job = make_client(FakeLLM(error="llm_timeout")).post("/ingest?wait=30", json=BODY, headers=AUTH).json()
    assert job["status"] == "failed" and job["error"] == "llm_timeout" and job["ok"] is False
    assert job["summary"] == "Failed: llm_timeout"


def test_invalid_event_is_not_ok(make_client):
    llm = FakeLLM([raw_event(start="2020-01-01T10:00", end=None)])
    job = make_client(llm).post("/ingest?wait=30", json=BODY, headers=AUTH).json()
    assert job["status"] == "done" and job["ok"] is False
    assert job["summary"] == "Skipped (in the past): Dentist"


def test_validation_errors(make_client):
    client = make_client()
    for body in ({"text": ""}, {"text": "x", "timezone": "Not/AZone"}, {"text": "x", "source": "Bad Source"},
                 {"text": "x", "unexpected": 1}, {}):
        assert client.post("/ingest", json=body, headers=AUTH).status_code == 422, body
    assert client.post("/ingest", content=b"not json", headers=AUTH).status_code == 422


def test_body_size_limit(make_client):
    resp = make_client().post("/ingest", json={"text": "x" * 5000}, headers=AUTH)
    assert resp.status_code == 413


def test_wait_is_capped(make_client):
    assert make_client().post("/ingest?wait=600", json=BODY, headers=AUTH).status_code == 422


def test_rate_limit(make_client):
    client = make_client(rate_limit_per_hour=2)
    assert client.post("/ingest", json=BODY, headers=AUTH).status_code in (200, 202)
    assert client.post("/ingest", json=BODY, headers=AUTH).status_code in (200, 202)
    resp = client.post("/ingest", json=BODY, headers=AUTH)
    assert resp.status_code == 429 and int(resp.headers["retry-after"]) > 0


def test_unknown_job(make_client):
    assert make_client().get("/jobs/does-not-exist", headers=AUTH).status_code == 404


def test_healthz(make_client):
    assert make_client().get("/healthz").json() == {"status": "ok"}
    resp = make_client(FakeLLM(error="llm_unreachable")).get("/healthz")
    assert resp.status_code == 503 and resp.json() == {"status": "degraded"}


def test_no_docs_endpoints(make_client):
    client = make_client()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_unfinished_jobs_resume_after_restart(make_client, tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    job_id = store.create(IngestRequest.model_validate(BODY))
    store.mark_running(job_id)  # as if the service died mid-job

    client = make_client()
    for _ in range(50):
        job = client.get(f"/jobs/{job_id}", headers=AUTH).json()
        if job["status"] == "done":
            break
        time.sleep(0.1)
    assert job["status"] == "done" and job["ok"]
