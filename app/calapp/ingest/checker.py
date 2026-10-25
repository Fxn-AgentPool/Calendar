"""Confidence check of extracted events with a decision model (Kev-4B).

Asks one multiple-choice question per event through llama.cpp's
``/v1/systemone`` endpoint: which of several candidate start times does the
text mean? The probability of the extracted one is the confidence.
Standard library only, like llm.py.
"""

from __future__ import annotations

import hashlib
import json
import random
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

from calapp.ingest.llm import day_table


class CheckerError(Exception):
    pass


def _label(value: datetime | date) -> str:
    if isinstance(value, datetime):
        return f"{value:%A %d %B %Y, %H:%M}"
    return f"{value:%A %d %B %Y}"


def candidates(start: datetime | date) -> list[datetime | date]:
    """The extracted start first, then near misses."""
    if isinstance(start, datetime):
        return [start, start + timedelta(days=1), start - timedelta(days=1), start + timedelta(hours=1)]
    return [start, start - timedelta(days=1), start + timedelta(days=1), start + timedelta(days=2)]


def build_request(text: str, reference: datetime, title: str, start: datetime | date) -> tuple[dict, str]:
    """The /v1/systemone body and the label of the extracted option.

    ``start`` is local time (a naive or user-zone datetime, or a date).
    """
    state = (
        f"Message received on {reference:%A, %d %B %Y} at {reference:%H:%M}.\n"
        f"Day table:\n{day_table(reference)}\n\nMessage:\n{text}"
    )
    labels = [_label(c) for c in candidates(start)]
    extracted = labels[0]
    # Shuffle (deterministically) so the extracted option isn't always first.
    seed = int(hashlib.sha256(f"{title}|{extracted}".encode()).hexdigest()[:8], 16)
    random.Random(seed).shuffle(labels)
    question = {
        "type": "choice",
        "instructions": f"When does '{title}' start, according to the message?",
        "criteria": {label: None for label in labels},
    }
    return {"state": state, "questions": {"start": question}}, extracted


class Checker:
    def __init__(self, base_url: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def confidence(self, text: str, reference: datetime, title: str, start: datetime | date) -> float:
        body, extracted = build_request(text, reference, title, start)
        req = urllib.request.Request(
            self.base_url + "/v1/systemone",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                answer = json.loads(resp.read())["answers"]["start"]
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise CheckerError(f"checker unreachable: {e}") from e
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise CheckerError("unexpected checker response") from e
        # "probabilities" maps each option to its probability. (The answer's
        # "confidence" field is something else: 0 means all options equally likely.)
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict) or extracted not in probabilities:
            raise CheckerError("checker answer has no probability for the extracted option")
        return float(probabilities[extracted])

    def healthy(self) -> bool:
        try:
            with urllib.request.urlopen(self.base_url + "/health", timeout=3) as resp:
                return json.loads(resp.read()).get("status") == "ok"
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
            return False
