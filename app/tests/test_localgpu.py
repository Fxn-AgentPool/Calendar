"""The desktop worker's decision tree (clients/localgpu.py) with a fake GPU and model."""

import sys
import threading
import time

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO / "clients"))
import localgpu as lg  # noqa: E402

EVENT = {"title": "Kino", "all_day": False, "start": "2026-11-05T20:30", "end": "2026-11-05T22:30",
         "timezone": None, "location": "Delphi", "notes": None}


class FakeGpu:
    def __init__(self):
        self.state = lg.GpuState(load=0, memory_used=1000, memory_total=16000)

    def __call__(self, own_pids=None):
        return self.state


class FakeModel:
    """Answers with ``result``; with ``block`` set, waits until released or stopped."""

    def __init__(self, result=None):
        self.result = result if result is not None else {"events": [EVENT]}
        self.running = False
        self.pid = None
        self.block = None
        self.calls = []
        self.stopped = threading.Event()

    def start(self):
        self.running, self.pid = True, 4242
        self.stopped.clear()

    def stop(self):
        self.running, self.pid = False, None
        self.stopped.set()

    def chat(self, messages, schema):
        self.calls.append((messages, schema))
        if self.block is not None:
            while not self.block.is_set():
                if self.stopped.wait(0.05):
                    raise lg.ModelError("killed")
        return self.result


class FakeServer:
    def __init__(self):
        self.texts, self.events, self.results = [], [], []
        self.review = None

    def make(self):
        return lg.Server(
            send_text=lambda text, at: self.texts.append(text) or "Added: sent as text",
            send_events=lambda events, at, text, by: self.events.append((events, text, by)) or "Added: Kino",
            next_review=lambda: self.review,
            review_result=lambda rid, answer: self.results.append((rid, answer)),
        )


@pytest.fixture
def setup(tmp_path):
    gpu, model, server, notes = FakeGpu(), FakeModel(), FakeServer(), []
    worker = lg.Worker(lg.DiskQueue(tmp_path), lg.GpuMonitor(sample=gpu, window=2), model, server.make(),
                       lambda m, t="": notes.append((t, m)), timezone_name="Europe/Berlin", model_name="Qwen3-VL 8B")
    return worker, gpu, model, server, notes


def busy_load(gpu):
    gpu.state = lg.GpuState(load=90, memory_used=9000, memory_total=16000)


def test_gpu_free_text_is_processed_locally(setup):
    worker, gpu, model, server, notes = setup
    worker.queue.add("text", text="Kino Donnerstag 20:30", note="mit Anna")
    worker.step()
    assert len(worker.queue) == 0
    (events, text, by), = server.events
    assert events == [EVENT] and by == "Qwen3-VL 8B"
    assert "My note: mit Anna" in text and "Kino Donnerstag" in model.calls[0][0][1]["content"]
    assert model.calls[0][1] is lg.EXTRACTION_SCHEMA
    assert notes[-1] == ("Added to calendar", "Added: Kino")


def test_gpu_free_image_is_processed_with_image_schema(setup):
    worker, gpu, model, server, notes = setup
    worker.queue.add("image", note="WhatsApp", image_png=b"\x89PNG fake")
    worker.step()
    messages, schema = model.calls[0]
    assert schema is lg.IMAGE_EXTRACTION_SCHEMA
    parts = messages[1]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "My note about it: WhatsApp" in parts[0]["text"]
    assert server.events and len(worker.queue) == 0


def test_screenshot_without_appointment_is_dropped(setup):
    worker, gpu, model, server, notes = setup
    model.result = {"events": []}
    worker.queue.add("image", image_png=b"png")
    worker.step()
    assert len(worker.queue) == 0 and not server.events
    assert notes[-1] == ("Nothing added", "No appointment found in the screenshot.")


def test_gpu_busy_sends_text_to_server_and_keeps_images(setup):
    worker, gpu, model, server, notes = setup
    worker.queue.add("text", text="Lunch Friday 12:00")
    worker.queue.add("image", image_png=b"png")
    gpu.state = lg.GpuState(load=5, memory_used=9000, memory_total=16000, others=["blender.exe"])
    model.start()
    worker.step()
    assert not model.running  # unloaded to free VRAM
    assert server.texts == ["Lunch Friday 12:00"] and not server.events
    assert [i["kind"] for i in worker.queue.items()] == ["image"]
    assert "blender.exe is using the GPU" in worker.status


def test_sustained_load_counts_as_busy_but_a_spike_does_not(setup):
    worker, gpu, model, server, notes = setup
    monitor = worker.monitor
    gpu.state = lg.GpuState(load=95, memory_used=2000, memory_total=16000)
    assert monitor.busy(working=False) is None  # one sample: not yet
    assert monitor.busy(working=False) == "GPU load 95%"
    assert monitor.busy(working=True) is None  # our own load while working is expected


def test_not_enough_vram_before_loading(setup):
    worker, gpu, model, server, notes = setup
    gpu.state = lg.GpuState(load=0, memory_used=9000, memory_total=16000)
    assert worker.monitor.busy(working=False) == "not enough free VRAM (7000 MiB)"
    worker.monitor.own_pids = {4242}  # once our model is loaded, its VRAM is ours
    assert worker.monitor.busy(working=False) is None


def test_no_nvidia_gpu_means_busy(tmp_path):
    monitor = lg.GpuMonitor(sample=lambda own_pids=None: None)
    assert monitor.busy(working=False) == "no NVIDIA GPU found (nvidia-smi)"


def test_gpu_wanted_mid_request_cancels_and_keeps_item(setup):
    worker, gpu, model, server, notes = setup
    model.block = threading.Event()
    worker.queue.add("image", image_png=b"png")
    thread = threading.Thread(target=worker.step)
    thread.start()
    time.sleep(0.3)
    gpu.state = lg.GpuState(load=99, memory_used=15000, memory_total=16000, others=["blender.exe"])
    thread.join(5)
    assert not thread.is_alive()
    assert model.stopped.is_set() and not model.running
    assert len(worker.queue) == 1 and not server.events
    assert worker.status == "cancelled: GPU needed elsewhere"


def test_offer_compute_works_through_review_queue(setup):
    worker, gpu, model, server, notes = setup
    server.review = {"id": "r1", "messages": [{"role": "user", "content": "check"}], "schema": {"type": "object"}}
    worker.step()
    assert not server.results  # compute not offered
    worker.offer_compute = True
    worker.step()
    assert server.results == [("r1", {"events": [EVENT]})]


def test_queue_first_then_reviews(setup):
    worker, gpu, model, server, notes = setup
    worker.offer_compute = True
    server.review = {"id": "r1", "messages": [], "schema": {}}
    worker.queue.add("text", text="Dentist Tuesday 14:00")
    worker.step()
    assert server.events and not server.results


def test_server_down_keeps_item_and_retries_later(setup):
    worker, gpu, model, server, notes = setup

    def down(*args):
        raise OSError("connection refused")

    worker.server.send_events = down
    worker.queue.add("text", text="Dentist Tuesday 14:00")
    worker.step()
    assert len(worker.queue) == 1 and notes[-1][0] == "Queued"
    worker.step()
    assert worker.status == "waiting to retry"


def test_disk_queue_survives_restart(tmp_path):
    q = lg.DiskQueue(tmp_path)
    a = q.add("text", text="one")
    q.add("image", image_png=b"img")
    again = lg.DiskQueue(tmp_path)
    items = again.items()
    assert [i["kind"] for i in items] == ["text", "image"] and items[0]["id"] == a
    assert again.image(items[1]) == b"img"
    again.remove(items[1])
    assert len(again) == 1 and not list(tmp_path.glob("*.png"))


def test_nvidia_smi_parsing(tmp_path, monkeypatch):
    def fake_run(args, **kwargs):
        class R:
            returncode = 0
            stderr = ""
        r = R()
        if "--query-gpu=utilization.gpu,memory.used,memory.total" in args:
            r.stdout = "37, 6012, 16303\n"
        else:
            r.stdout = "4242, C:\\llama\\llama-server.exe\n777, C:\\Program Files\\Blender\\blender.exe\n"
        return r

    monkeypatch.setattr(lg.subprocess, "run", fake_run)
    state = lg.query_gpu(own_pids={4242})
    assert (state.load, state.memory_used, state.memory_total) == (37, 6012, 16303)
    assert state.others == ["blender.exe"]
