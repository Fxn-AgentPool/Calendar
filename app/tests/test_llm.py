"""LLMClient against a stub of llama.cpp's HTTP API."""

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

import pytest

from calapp.ingest.llm import EXTRACTION_SCHEMA, LLMClient, LLMError, build_messages, day_table


class Stub:
    def __init__(self):
        self.response: dict | bytes = {}
        self.status = 200
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self._reply({"status": "ok"})

            def do_POST(self):
                stub.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self._reply(stub.response, stub.status)

            def _reply(self, body, status=200):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.server.shutdown()


def completion(content: str, finish: str = "stop") -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20}}


REF = datetime(2026, 10, 23, 9, 0, tzinfo=ZoneInfo("Europe/Berlin"))


def test_sends_schema_and_parses_events(stub):
    stub.response = completion(json.dumps({"events": [{"title": "Dentist", "start": "2026-10-27T14:00"}]}))
    result = LLMClient(stub.url).extract("text", REF, "Europe/Berlin", "email")
    assert result.events == [{"title": "Dentist", "start": "2026-10-27T14:00"}]
    assert result.prompt_tokens == 100
    sent = stub.requests[0]
    assert sent["temperature"] == 0
    assert sent["response_format"]["json_schema"]["schema"] == EXTRACTION_SCHEMA


def test_prompt_contains_reference_date_and_text():
    messages = build_messages("Lunch Friday", REF, "Europe/Berlin", "chat")
    user = messages[1]["content"]
    assert "Today: Fri 2026-10-23, current time 09:00" in user
    assert "Europe/Berlin" in user and "Source: chat" in user
    assert "<text>\nLunch Friday\n</text>" in user


def test_day_table_lists_weekdays_around_today():
    table = day_table(REF).splitlines()
    assert table[0] == "2026-10-22 Thursday (yesterday)"
    assert table[1] == "2026-10-23 Friday (today)"
    assert table[2] == "2026-10-24 Saturday (tomorrow)"
    assert table[-1] == "2026-11-12 Thursday"


@pytest.mark.parametrize("response, code", [
    (completion("{}", finish="length"), "llm_bad_output"),
    (completion("not json"), "llm_bad_output"),
    (completion('{"something": []}'), "llm_bad_output"),
    ({"unexpected": True}, "llm_bad_output"),
])
def test_bad_output(stub, response, code):
    stub.response = response
    with pytest.raises(LLMError) as e:
        LLMClient(stub.url).extract("text", REF, "Europe/Berlin")
    assert e.value.code == code


def test_http_error(stub):
    stub.status, stub.response = 503, {"error": "loading model"}
    with pytest.raises(LLMError) as e:
        LLMClient(stub.url).extract("text", REF, "Europe/Berlin")
    assert e.value.code == "llm_error"


def test_unreachable():
    with pytest.raises(LLMError) as e:
        LLMClient("http://127.0.0.1:9", timeout=2).extract("text", REF, "Europe/Berlin")
    assert e.value.code == "llm_unreachable"
    assert not LLMClient("http://127.0.0.1:9").healthy()


def test_healthy(stub):
    assert LLMClient(stub.url).healthy()
