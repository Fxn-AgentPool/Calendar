"""Local GPU processing for the tray app (docs/CLIENTS.md, "Local GPU").

Decision tree, re-evaluated every second by Worker:

* GPU busy (another program computes on it, or high load while we're idle):
  stop our model at once (frees all its VRAM), send waiting *text* items to
  the server as plain text, keep images queued.
* GPU free and something queued: process it locally, send the events to the
  server (POST /events).
* GPU free, queue empty, "offer compute" on: work through the server's review
  queue (GET /reviews/next, POST /reviews/<id>/result).
* While working, a guard thread keeps watching; if the GPU is wanted, the
  model is killed mid-request and the item stays queued.

Standard library only; the prompt and schemas come from app/calapp/ingest/llm.py.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
for candidate in (ROOT.parent / "app", ROOT / "app"):  # repo checkout / PyInstaller bundle
    if (candidate / "calapp").is_dir():
        sys.path.insert(0, str(candidate))
from calapp.ingest.llm import (  # noqa: E402
    EXTRACTION_SCHEMA,
    IMAGE_EXTRACTION_SCHEMA,
    build_image_messages,
    build_messages,
)

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


# --- GPU monitor -------------------------------------------------------------------

@dataclass
class GpuState:
    load: int  # percent
    memory_used: int  # MiB, whole GPU
    memory_total: int  # MiB
    others: list[str] = field(default_factory=list)  # other processes computing on the GPU


def query_gpu(smi: str = "nvidia-smi", own_pids: set[int] | None = None) -> GpuState | None:
    """One nvidia-smi sample, or None if there is no usable NVIDIA GPU."""
    own_pids = own_pids or set()

    def run(*args: str) -> list[str]:
        out = subprocess.run([smi, *args, "--format=csv,noheader,nounits"], capture_output=True, text=True,
                             timeout=10, creationflags=NO_WINDOW)
        if out.returncode != 0:
            raise OSError(out.stderr.strip() or "nvidia-smi failed")
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]

    try:
        load, used, total = (int(float(v)) for v in run("--query-gpu=utilization.gpu,memory.used,memory.total")[0]
                             .split(","))
        others = []
        for line in run("--query-compute-apps=pid,process_name"):
            pid, _, name = line.partition(",")
            if pid.strip().isdigit() and int(pid) not in own_pids:
                # Windows paths (C:\...\blender.exe): keep just the program name.
                others.append(name.strip().replace("\\", "/").rsplit("/", 1)[-1] or pid.strip())
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None
    return GpuState(load, used, total, others)


class GpuMonitor:
    """Decides whether the GPU is free for us."""

    def __init__(self, sample=query_gpu, load_threshold: int = 30, need_mib: int = 11_000, window: int = 3):
        self.sample = sample
        self.load_threshold = load_threshold
        self.need_mib = need_mib
        self.loads: deque[int] = deque(maxlen=window)
        self.own_pids: set[int] = set()  # our model server, while it runs

    def busy(self, working: bool) -> str | None:
        """Why the GPU is not available (None if it is). ``working``: our model is computing."""
        state = self.sample(own_pids=self.own_pids)
        if state is None:
            return "no NVIDIA GPU found (nvidia-smi)"
        if state.others:
            return f"{', '.join(sorted(set(state.others)))} is using the GPU"
        self.loads.append(state.load)
        # Our own load is expected while working; when idle, sustained load means a game/render.
        if not working and len(self.loads) == self.loads.maxlen and min(self.loads) >= self.load_threshold:
            return f"GPU load {min(self.loads)}%"
        # Only matters before loading; once loaded, the VRAM is ours.
        free = state.memory_total - state.memory_used
        if not self.own_pids and free < self.need_mib:
            return f"not enough free VRAM ({free} MiB)"
        return None


# --- Local model (llama.cpp server, started and killed by us) ------------------------

class ModelError(Exception):
    pass


class LocalModel:
    def __init__(self, server: str, model: str, mmproj: str | None = None, port: int = 8091,
                 ctx: int = 16384, extra_args: list[str] | None = None):
        self.server, self.model, self.mmproj = server, model, mmproj
        self.port, self.ctx = port, ctx
        self.extra_args = extra_args or []
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.running else None

    def start(self, timeout: float = 180) -> None:
        with self.lock:
            if self.running:
                return
            for path in (self.server, self.model, self.mmproj):
                if path and not Path(path).is_file():
                    raise ModelError(f"file not found: {path}")
            args = [self.server, "--model", self.model, "--host", "127.0.0.1", "--port", str(self.port),
                    "--ctx-size", str(self.ctx), "--n-gpu-layers", "999", "--parallel", "1", "--no-webui",
                    *self.extra_args]
            if self.mmproj:
                args += ["--mmproj", self.mmproj]
            self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            creationflags=NO_WINDOW)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running:
                raise ModelError("the local model server exited while loading")
            try:
                with urllib.request.urlopen(self.url + "/health", timeout=2) as resp:
                    if json.loads(resp.read()).get("status") == "ok":
                        return
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.5)
        self.stop()
        raise ModelError("the local model did not load in time")

    def stop(self) -> None:
        """Kill the server: frees all of its VRAM immediately. Safe from any thread."""
        process = self.process
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                pass

    def chat(self, messages: list[dict], schema: dict, max_tokens: int = 1024) -> dict:
        body = {"messages": messages, "temperature": 0, "max_tokens": max_tokens,
                "response_format": {"type": "json_schema", "json_schema": {"name": "events", "schema": schema}}}
        req = urllib.request.Request(self.url + "/v1/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read())
            choice = data["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ModelError("output was cut off")
            return json.loads(choice["message"]["content"])
        except (urllib.error.URLError, OSError, ConnectionError) as e:
            raise ModelError(f"local model unavailable: {e}") from e
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise ModelError("local model returned unusable output") from e


# --- Disk queue ----------------------------------------------------------------------

class DiskQueue:
    """One JSON file per item (plus a PNG for screenshots), oldest first."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def add(self, kind: str, text: str = "", note: str = "", image_png: bytes | None = None,
            timezone_name: str | None = None) -> str:
        item_id = f"{time.time_ns()}-{secrets.token_hex(3)}"
        if image_png is not None:
            (self.folder / f"{item_id}.png").write_bytes(image_png)
        item = {"id": item_id, "kind": kind, "text": text, "note": note, "timezone": timezone_name,
                "received_at": datetime.now(timezone.utc).astimezone().isoformat()}
        tmp = self.folder / f"{item_id}.json.tmp"
        tmp.write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.folder / f"{item_id}.json")
        return item_id

    def items(self) -> list[dict]:
        result = []
        for path in sorted(self.folder.glob("*.json")):
            try:
                result.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return result

    def image(self, item: dict) -> bytes:
        return (self.folder / f"{item['id']}.png").read_bytes()

    def remove(self, item: dict) -> None:
        for suffix in (".json", ".png"):
            (self.folder / f"{item['id']}{suffix}").unlink(missing_ok=True)

    def __len__(self) -> int:
        return len(list(self.folder.glob("*.json")))


# --- Worker -------------------------------------------------------------------------

class Cancelled(Exception):
    """The GPU was needed elsewhere; the model was stopped mid-request."""


@dataclass
class Server:
    """What the worker needs from the ingest server (functions supplied by the app)."""

    send_text: callable  # (text, received_at) -> summary, raises on network errors
    send_events: callable  # (events, received_at, text, extracted_by) -> summary
    next_review: callable  # () -> task dict or None (None also when compute isn't offered)
    review_result: callable  # (review_id, answer) -> None


class Worker(threading.Thread):
    IDLE_UNLOAD_SECONDS = 120
    RETRY_SECONDS = 60

    def __init__(self, queue: DiskQueue, monitor: GpuMonitor, model: LocalModel, server: Server, notify,
                 timezone_name: str | None = None, model_name: str = "the desktop model", tick: float = 1.0):
        super().__init__(daemon=True)
        self.queue, self.monitor, self.model, self.server = queue, monitor, model, server
        self.notify = notify
        self.timezone_name = timezone_name
        self.model_name = model_name
        self.tick = tick
        self.offer_compute = False
        self.status = "starting"
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_work = time.monotonic()
        self._retry_after = 0.0
        self._cancelled = threading.Event()

    # -- control
    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.model.stop()

    def wake(self) -> None:
        self._wake.set()

    def gpu_free(self) -> bool:
        return self.monitor.busy(working=False) is None

    # -- loop
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as e:  # never let the worker die
                self.status = f"error: {e}"
            self._wake.wait(self.tick)
            self._wake.clear()
        self.model.stop()

    def step(self) -> None:
        self.monitor.own_pids = {self.model.pid} if self.model.pid else set()
        reason = self.monitor.busy(working=False)
        if reason:
            self.status = f"GPU busy: {reason}"
            if self.model.running:
                self.model.stop()
            self._send_texts_to_server()
            return
        if time.monotonic() < self._retry_after:
            self.status = "waiting to retry"
            return
        items = self.queue.items()
        if items:
            self._process(items[0])
            return
        if self.offer_compute:
            task = self._safe(self.server.next_review)
            if task:
                self._review(task)
                return
        self.status = "idle"
        if self.model.running and time.monotonic() - self._last_work > self.IDLE_UNLOAD_SECONDS:
            self.model.stop()

    def _safe(self, func, *args):
        try:
            return func(*args)
        except Exception:
            self._retry_after = time.monotonic() + self.RETRY_SECONDS
            return None

    def _send_texts_to_server(self) -> None:
        """GPU not available: text goes to the server as is; images wait."""
        for item in self.queue.items():
            if item["kind"] != "text" or time.monotonic() < self._retry_after:
                continue
            text = item["text"] if not item["note"] else f"My note: {item['note']}\n\nText:\n{item['text']}"
            try:
                summary = self.server.send_text(text, item["received_at"])
            except Exception as e:
                self._retry_after = time.monotonic() + self.RETRY_SECONDS
                self.notify(f"Server not reachable, will retry: {e}", "Queued")
                return
            self.queue.remove(item)
            self.notify(summary, "Sent to the server")

    def _guarded(self, func, *args):
        """Run func while a guard watches the GPU; kills the model if it's wanted elsewhere."""
        self._cancelled.clear()
        done = threading.Event()

        def guard():
            while not done.wait(1.0):
                self.monitor.own_pids = {self.model.pid} if self.model.pid else set()
                if self.monitor.busy(working=True):
                    self._cancelled.set()
                    self.model.stop()
                    return

        threading.Thread(target=guard, daemon=True).start()
        try:
            self.model.start()
            return func(*args)
        except ModelError:
            if self._cancelled.is_set():
                raise Cancelled() from None
            raise
        finally:
            done.set()
            self._last_work = time.monotonic()

    def _process(self, item: dict) -> None:
        received = datetime.fromisoformat(item["received_at"])
        tz = item.get("timezone") or self.timezone_name or "UTC"
        reference = received.astimezone(ZoneInfo(tz))
        if item["kind"] == "image":
            messages = build_image_messages(self.queue.image(item), reference, tz, item["note"] or None)
            schema, label = IMAGE_EXTRACTION_SCHEMA, "screenshot"
            source_text = "Screenshot" + (f", note: {item['note']}" if item["note"] else "")
        else:
            text = item["text"] if not item["note"] else f"My note: {item['note']}\n\nText:\n{item['text']}"
            messages = build_messages(text, reference, tz, None)
            schema, label, source_text = EXTRACTION_SCHEMA, "text", text
        self.status = f"processing {label}"
        try:
            result = self._guarded(self.model.chat, messages, schema)
        except Cancelled:
            self.status = "cancelled: GPU needed elsewhere"
            return  # stays queued
        except ModelError as e:
            self.notify(f"Local model failed: {e}", "Queued")
            self._retry_after = time.monotonic() + self.RETRY_SECONDS
            return
        events = [e for e in result.get("events", []) if isinstance(e, dict)]
        if not events:
            self.queue.remove(item)
            self.notify(f"No appointment found in the {label}.", "Nothing added")
            return
        try:
            summary = self.server.send_events(events, item["received_at"], source_text, self.model_name)
        except Exception as e:
            self._retry_after = time.monotonic() + self.RETRY_SECONDS
            self.notify(f"Server not reachable, will retry: {e}", "Queued")
            return
        self.queue.remove(item)
        self.notify(summary, "Added to calendar")

    def _review(self, task: dict) -> None:
        self.status = "re-checking an uncertain event for the server"
        try:
            answer = self._guarded(self.model.chat, task["messages"], task["schema"])
        except (Cancelled, ModelError):
            return  # the server hands the item out again after its lease
        self._safe(self.server.review_result, task["id"], answer)
