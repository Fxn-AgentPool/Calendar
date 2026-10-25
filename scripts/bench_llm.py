#!/usr/bin/env python3
"""Measure speed and extraction quality of the running LLM.

Runs every sample in scripts/samples/ through the same prompt and schema the
ingest service uses, and compares the result with the expected events.

    python3 scripts/bench_llm.py                       # LLM on 127.0.0.1:8080
    python3 scripts/bench_llm.py --url http://host:8080 --repeat 3

To compare models: change LLM_MODEL_FILE in .env, `docker compose up -d llm`,
wait until it is healthy, and run this again.

Needs only the Python standard library.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

from calapp.ingest.llm import LLMClient, LLMError  # noqa: E402


def matches(expected: dict, got: dict) -> bool:
    if got.get("start") != expected["start"]:
        return False
    if expected.get("end") is not None and got.get("end") != expected["end"]:
        return False
    if bool(got.get("all_day")) != expected["all_day"]:
        return False
    if expected.get("timezone") and got.get("timezone") != expected["timezone"]:
        return False
    title = (got.get("title") or "").lower()
    return any(word in title for word in expected["title_any"])


def score(expected: list[dict], got: list[dict]) -> tuple[int, int, int]:
    """Returns (correct, missed, extra)."""
    remaining = list(got)
    correct = 0
    for exp in expected:
        for i, g in enumerate(remaining):
            if matches(exp, g):
                correct += 1
                del remaining[i]
                break
    return correct, len(expected) - correct, len(remaining)


def model_name(client: LLMClient) -> str:
    try:
        models = client._request("/v1/models", timeout=5)
        return models["data"][0]["id"]
    except (LLMError, KeyError, IndexError, TypeError):
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8080", help="llama.cpp server URL")
    parser.add_argument("--samples", default=str(ROOT / "scripts" / "samples"), help="folder with sample .json files")
    parser.add_argument("--repeat", type=int, default=1, help="runs per sample (timings use the median)")
    parser.add_argument("--verbose", "-v", action="store_true", help="print the extracted events")
    args = parser.parse_args()

    client = LLMClient(args.url, timeout=600)
    if not client.healthy():
        print(f"LLM at {args.url} is not healthy (still loading the model?)", file=sys.stderr)
        return 1

    files = sorted(Path(args.samples).glob("*.json"))
    if not files:
        print(f"no samples in {args.samples}", file=sys.stderr)
        return 1

    print(f"model: {model_name(client)}\n")
    print(f"{'sample':32} {'ok':>5} {'miss':>4} {'extra':>5} {'secs':>6} {'in tok':>6} {'out tok':>7}")

    totals = {"correct": 0, "missed": 0, "extra": 0, "expected": 0}
    all_seconds: list[float] = []
    prompt_rates: list[float] = []
    for path in files:
        sample = json.loads(path.read_text())
        reference = datetime.fromisoformat(sample["received_at"])
        runs = []
        for _ in range(args.repeat):
            try:
                runs.append(client.extract(sample["text"], reference, sample["timezone"], sample.get("source")))
            except LLMError as e:
                print(f"{path.stem:32} error: {e}")
                break
        if not runs:
            totals["missed"] += len(sample["expected"])
            totals["expected"] += len(sample["expected"])
            continue
        result = runs[-1]
        correct, missed, extra = score(sample["expected"], result.events)
        seconds = statistics.median(r.seconds for r in runs)
        all_seconds.append(seconds)
        if result.prompt_tokens:
            prompt_rates.append((result.prompt_tokens + (result.completion_tokens or 0)) / seconds)
        totals["correct"] += correct
        totals["missed"] += missed
        totals["extra"] += extra
        totals["expected"] += len(sample["expected"])
        print(
            f"{path.stem:32} {correct:>2}/{len(sample['expected']):<2} {missed:>4} {extra:>5} "
            f"{seconds:>6.1f} {result.prompt_tokens or 0:>6} {result.completion_tokens or 0:>7}"
        )
        if args.verbose:
            for event in result.events:
                print("    ", json.dumps(event, ensure_ascii=False))

    print()
    print(f"correct: {totals['correct']}/{totals['expected']}, missed: {totals['missed']}, extra: {totals['extra']}")
    if all_seconds:
        print(f"seconds per sample: median {statistics.median(all_seconds):.1f}, max {max(all_seconds):.1f}")
    return 0 if totals["missed"] == 0 and totals["extra"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
