#!/usr/bin/env python3
"""Minimal desktop window for sending text to the calendar (temporary).

    clients/calsend_gui.py

Paste text, pick a source hint, press Send (or Ctrl+Enter). Uses the same
configuration as calsend.py (CALSEND_URL / CALSEND_KEY, environment or
~/.config/calsend.conf). Needs tkinter: `sudo apt install python3-tk`.
"""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import scrolledtext, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import calsend  # noqa: E402

SOURCES = ["", "email", "whatsapp", "chat", "poll", "note"]


def main() -> None:
    config = calsend.load_config()
    url, key = config.get("CALSEND_URL"), config.get("CALSEND_KEY")

    root = tk.Tk()
    root.title("Add to calendar")
    root.geometry("560x420")

    text = scrolledtext.ScrolledText(root, wrap="word", height=12, undo=True)
    text.pack(fill="both", expand=True, padx=8, pady=(8, 4))
    text.focus_set()

    bar = ttk.Frame(root)
    bar.pack(fill="x", padx=8)
    ttk.Label(bar, text="Source:").pack(side="left")
    source = ttk.Combobox(bar, values=SOURCES, width=12, state="readonly")
    source.current(0)
    source.pack(side="left", padx=(4, 12))
    send_button = ttk.Button(bar, text="Send (Ctrl+Enter)")
    send_button.pack(side="right")
    ttk.Button(bar, text="Clear", command=lambda: text.delete("1.0", "end")).pack(side="right", padx=4)

    result = tk.Text(root, height=6, wrap="word", state="disabled")
    result.pack(fill="x", padx=8, pady=8)

    def show(message: str) -> None:
        result.configure(state="normal")
        result.delete("1.0", "end")
        result.insert("1.0", message)
        result.configure(state="disabled")

    if not url or not key:
        show(f"Not configured: set CALSEND_URL and CALSEND_KEY in {calsend.CONFIG}")
        send_button.state(["disabled"])

    results: queue.Queue[str] = queue.Queue()

    def worker(body: str, hint: str | None) -> None:
        try:
            code, job = calsend.send(url, key, body, hint)
            message = job.get("summary") or job.get("status") or "done"
            if code == 2:
                message += "\n(some events were not added)"
            elif code == 1:
                message = "Nothing added:\n" + message
        except calsend.SendError as e:
            message = f"Failed: {e}"
        results.put(message)

    def poll() -> None:
        try:
            message = results.get_nowait()
        except queue.Empty:
            root.after(200, poll)
            return
        show(message)
        send_button.state(["!disabled"])

    def on_send(_event=None) -> str:
        body = text.get("1.0", "end").strip()
        if not body or send_button.instate(["disabled"]):
            return "break"
        send_button.state(["disabled"])
        show("Sending… (the server needs about 20–40 s)")
        threading.Thread(target=worker, args=(body, source.get() or None), daemon=True).start()
        root.after(200, poll)
        return "break"

    send_button.configure(command=on_send)
    text.bind("<Control-Return>", on_send)  # on the text box, so no newline is inserted
    root.mainloop()


if __name__ == "__main__":
    main()
