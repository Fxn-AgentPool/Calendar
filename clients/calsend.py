#!/usr/bin/env python3
"""Send text to the calendar ingest API and print what was added.

    calsend "Dentist moved to Tuesday 14:00"
    pbpaste | calsend --source email
    calsend --file chat.txt --source whatsapp

Configuration (environment variables, or KEY=value lines in
~/.config/calsend.conf):

    CALSEND_URL   e.g. https://ingest.example.com
    CALSEND_KEY   the API key from scripts/new_api_key.sh

Exit codes: 0 everything added (or already there), 1 failed or nothing
added, 2 some events were not added (conflict / skipped), 3 usage or
configuration error.

Needs only the Python standard library (3.8+).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CONFIG = Path.home() / ".config" / "calsend.conf"
WAIT_SECONDS = 60
POLL_FOR_SECONDS = 600
POLL_INTERVAL = 5


def load_config() -> dict[str, str]:
    config: dict[str, str] = {}
    if CONFIG.exists():
        for line in CONFIG.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                config[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("CALSEND_URL", "CALSEND_KEY"):
        if os.environ.get(key):
            config[key] = os.environ[key]
    return config


def request(method: str, url: str, key: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "User-Agent": "calsend",
    })
    try:
        with urllib.request.urlopen(req, timeout=WAIT_SECONDS + 30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            detail = {}
        return e.code, detail


class SendError(Exception):
    """Sending failed; ``code`` is the exit code to use."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def send(url: str, key: str, text: str, source: str | None = None) -> tuple[int, dict]:
    """Send text, wait for the result. Returns (exit code, job). Raises SendError."""
    body = {"text": text, "received_at": datetime.now(timezone.utc).astimezone().isoformat()}
    if source:
        body["source"] = source
    base = url.rstrip("/")
    try:
        status, job = request("POST", f"{base}/ingest?wait={WAIT_SECONDS}", key, body)
        # Still running after the wait: poll until it finishes.
        deadline = time.monotonic() + POLL_FOR_SECONDS
        while status == 202 and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            status, job = request("GET", f"{base}/jobs/{job['job_id']}", key)
            if job.get("status") in ("pending", "running"):
                status = 202
    except (urllib.error.URLError, OSError) as e:
        raise SendError(f"could not reach {base}: {e}", 1) from e

    if status == 202:
        raise SendError(f"still processing (job {job.get('job_id')}); check later", 1)
    if status != 200:
        message = job.get("detail") if isinstance(job, dict) else None
        raise SendError(f"error {status}: {message or 'request failed'}", 3 if status in (401, 413, 422) else 1)
    if job.get("ok"):
        return 0, job
    outcomes = {e.get("outcome") for e in job.get("events", [])}
    return (2 if outcomes & {"added", "duplicate"} else 1), job


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send text to the calendar ingest API.")
    parser.add_argument("text", nargs="*", help="the text (default: read from --file or stdin)")
    parser.add_argument("--file", "-f", help="read the text from this file")
    parser.add_argument("--source", "-s", help="hint: email, whatsapp, chat, poll, note, …")
    parser.add_argument("--json", action="store_true", help="print the full JSON result")
    args = parser.parse_args(argv)

    config = load_config()
    url, key = config.get("CALSEND_URL"), config.get("CALSEND_KEY")
    if not url or not key:
        print(f"set CALSEND_URL and CALSEND_KEY (environment or {CONFIG})", file=sys.stderr)
        return 3

    if args.text:
        text = " ".join(args.text)
    elif args.file:
        text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        parser.error("no text given")
    if not text.strip():
        print("text is empty", file=sys.stderr)
        return 3

    try:
        code, job = send(url, key, text, args.source)
    except SendError as e:
        print(e, file=sys.stderr)
        return e.code
    if args.json:
        print(json.dumps(job, indent=2, ensure_ascii=False))
    else:
        print(job.get("summary") or job.get("status"))
    return code


if __name__ == "__main__":
    sys.exit(main())
