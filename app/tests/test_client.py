"""clients/calsend.py against a running ingest server (fake LLM, real Radicale)."""

import importlib.util
import socket
import threading
import time

import pytest
import uvicorn

from calapp.common.caldav import CalDAVClient
from calapp.ingest.main import create_app
from calapp.ingest.pipeline import Pipeline
from conftest import PASSWORDS, REPO, FakeLLM, raw_event
from test_api import KEY, settings

spec = importlib.util.spec_from_file_location("calsend", REPO / "clients" / "calsend.py")
calsend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(calsend)


@pytest.fixture
def serve(radicale, tmp_path, clean_calendar, monkeypatch):
    servers = []

    def start(llm):
        s = settings(radicale, tmp_path)
        calendar = CalDAVClient(radicale, "ingest", PASSWORDS["ingest"])
        pipeline = Pipeline(llm, calendar, s.calendar_path, s.caldav.timezone, llm_retry_delays=(0,))
        app = create_app(s, pipeline=pipeline, llm=llm, calendar=calendar)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        while not server.started:
            time.sleep(0.05)
        servers.append(server)
        monkeypatch.setenv("CALSEND_URL", f"http://127.0.0.1:{port}")
        monkeypatch.setenv("CALSEND_KEY", KEY)
        monkeypatch.setattr(calsend, "CONFIG", tmp_path / "no-config")

    yield start
    for server in servers:
        server.should_exit = True


def test_success(serve, capsys):
    serve(FakeLLM([raw_event(start="2026-12-01T14:00", end="2026-12-01T14:45")]))
    assert calsend.main(["Dentist", "1 Dec", "14:00"]) == 0
    assert capsys.readouterr().out.strip() == "Added: Dentist, Tue 01 Dec 14:00–14:45"


def test_polls_when_wait_runs_out(serve, capsys, monkeypatch):
    monkeypatch.setattr(calsend, "WAIT_SECONDS", 1)
    monkeypatch.setattr(calsend, "POLL_INTERVAL", 0.2)
    serve(FakeLLM([raw_event(start="2026-12-01T14:00", end=None)], delay=2))
    assert calsend.main(["Dentist"]) == 0
    assert "Added: Dentist" in capsys.readouterr().out


def test_partly_added(serve, capsys):
    serve(FakeLLM([raw_event(start="2026-12-01T14:00", end=None), raw_event(start="2001-01-01T10:00", end=None)]))
    assert calsend.main(["text"]) == 2
    out = capsys.readouterr().out
    assert "Added: Dentist" in out and "Skipped (in the past)" in out


def test_failure(serve, capsys):
    serve(FakeLLM(error="llm_timeout"))
    assert calsend.main(["text"]) == 1
    assert capsys.readouterr().out.strip() == "Failed: llm_timeout"


def test_wrong_key(serve, capsys, monkeypatch):
    serve(FakeLLM([raw_event()]))
    monkeypatch.setenv("CALSEND_KEY", "wrong")
    assert calsend.main(["text"]) == 3
    assert "error 401" in capsys.readouterr().err


def test_missing_config(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("CALSEND_URL", raising=False)
    monkeypatch.delenv("CALSEND_KEY", raising=False)
    monkeypatch.setattr(calsend, "CONFIG", tmp_path / "none")
    assert calsend.main(["text"]) == 3


def test_config_file(monkeypatch, tmp_path):
    monkeypatch.delenv("CALSEND_URL", raising=False)
    monkeypatch.delenv("CALSEND_KEY", raising=False)
    conf = tmp_path / "calsend.conf"
    conf.write_text('# comment\nCALSEND_URL="https://ingest.example.com"\nCALSEND_KEY=abc\n')
    monkeypatch.setattr(calsend, "CONFIG", conf)
    assert calsend.load_config() == {"CALSEND_URL": "https://ingest.example.com", "CALSEND_KEY": "abc"}
