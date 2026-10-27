#!/usr/bin/env python3
"""Calendar quick-add for Windows: a tray app with a global hotkey.

Select text anywhere and press the hotkey (default Win+C): a slim bar pops
up with a notes field. ✓ (Enter) sends the selection and the note to the
ingest API, ✗ (Esc) cancels. The result shows as a notification. Settings
(server, API key, hotkey, start with Windows) are in the tray icon's menu.

    pip install -r clients/requirements-windows.txt
    pythonw clients/calsend_tray.py

or build CalendarSend.exe with clients/build_windows.ps1. Settings are kept in
%APPDATA%\\CalendarSend\\settings.json; the API key is encrypted for the
current Windows user (DPAPI).
"""

from __future__ import annotations

import base64
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import calsend  # noqa: E402
import localgpu  # noqa: E402

APP = "CalendarSend"
IS_WINDOWS = sys.platform == "win32"
CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home() / ".config") / APP
SETTINGS_FILE = CONFIG_DIR / "settings.json"
DEFAULTS = {
    "url": "", "key": "", "hotkey": "win+c", "source": "", "autostart": False,
    # Local GPU processing (clients/localgpu.py)
    "screenshot_hotkey": "win+shift+c", "local_enabled": False, "llama_server": "", "model_file": "",
    "mmproj_file": "", "timezone": "Europe/Berlin", "gpu_load_threshold": 30,
    "offer_compute": False, "helper_key": "",
}
SOURCES = ["", "email", "whatsapp", "chat", "poll", "note"]

if IS_WINDOWS:
    import ctypes
    import winreg
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)


# --- Settings ----------------------------------------------------------------

if IS_WINDOWS:
    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
        """The blob and its buffer; keep the buffer referenced while the blob is used."""
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def protect(secret: str) -> str:
        """Encrypt for the current Windows user (DPAPI)."""
        blob, _buf = _blob(secret.encode())
        out = _Blob()
        if not crypt32.CryptProtectData(ctypes.byref(blob), None, None, None, None, 0, ctypes.byref(out)):
            raise ctypes.WinError(ctypes.get_last_error())
        data = ctypes.string_at(out.pbData, out.cbData)
        kernel32.LocalFree(out.pbData)
        return "dpapi:" + base64.b64encode(data).decode()

    def unprotect(stored: str) -> str:
        if not stored.startswith("dpapi:"):
            return stored
        blob, _buf = _blob(base64.b64decode(stored[6:]))
        out = _Blob()
        if not crypt32.CryptUnprotectData(ctypes.byref(blob), None, None, None, None, 0, ctypes.byref(out)):
            return ""  # e.g. settings copied from another user/PC
        data = ctypes.string_at(out.pbData, out.cbData)
        kernel32.LocalFree(out.pbData)
        return data.decode()
else:
    def protect(secret: str) -> str:
        return secret

    def unprotect(stored: str) -> str:
        return stored


def installed_local_files() -> dict:
    """Paths from clients/setup_local_gpu.ps1, if it was run (empty strings otherwise)."""
    base = Path(os.environ.get("LOCALAPPDATA") or CONFIG_DIR.parent) / APP
    found = {"llama_server": base / "llama" / "llama-server.exe"}
    models = sorted((base / "models").glob("Qwen3VL-*-Instruct-*.gguf")) if (base / "models").is_dir() else []
    found["model_file"] = next((m for m in models if "Q8_0" in m.name), models[0] if models else Path())
    mmproj = sorted((base / "models").glob("mmproj-*.gguf")) if (base / "models").is_dir() else []
    found["mmproj_file"] = mmproj[0] if mmproj else Path()
    return {k: str(v) if v.name and v.is_file() else "" for k, v in found.items()}


def load_settings() -> dict:
    settings = dict(DEFAULTS)
    settings.update(installed_local_files())
    try:
        settings.update({k: v for k, v in json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).items()
                         if v != "" or k not in ("llama_server", "model_file", "mmproj_file")})
    except (OSError, ValueError):
        pass
    settings["key"] = unprotect(settings.get("key", ""))
    settings["helper_key"] = unprotect(settings.get("helper_key", ""))
    return settings


def save_settings(settings: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    stored = dict(settings, key=protect(settings["key"]) if settings["key"] else "",
                  helper_key=protect(settings["helper_key"]) if settings.get("helper_key") else "")
    SETTINGS_FILE.write_text(json.dumps(stored, indent=2), encoding="utf-8")


# --- Hotkey --------------------------------------------------------------------

MODIFIERS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}


def parse_hotkey(text: str) -> tuple[int, int]:
    """'win+c' -> (modifier flags, virtual-key code). Raises ValueError."""
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError("use at least one modifier and a key, e.g. win+c or ctrl+alt+c")
    mods = 0
    for part in parts[:-1]:
        if part not in MODIFIERS:
            raise ValueError(f"unknown modifier {part!r} (use win, ctrl, alt, shift)")
        mods |= MODIFIERS[part]
    key = parts[-1]
    if len(key) == 1 and key.isalnum():
        return mods, ord(key.upper())
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return mods, 0x70 + int(key[1:]) - 1
    if key == "space":
        return mods, 0x20
    raise ValueError(f"unsupported key {key!r} (use a letter, digit, F1-F24 or space)")


class HotkeyThread(threading.Thread):
    """Catches the hotkey with a low-level keyboard hook and calls on_hotkey().

    A hook sees keys before Windows' own shortcut handling, so this also works
    for combinations Windows has taken (Win+C opens Copilot or Search on
    Windows 11): the key is swallowed and never reaches Windows.
    """

    WM_QUIT = 0x0012
    WH_KEYBOARD_LL = 13
    WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
    LLKHF_INJECTED = 0x10
    # Left/right modifier keys as the hook reports them -> modifier flag.
    MODIFIER_KEYS = {0x5B: 0x8, 0x5C: 0x8, 0xA2: 0x2, 0xA3: 0x2, 0xA4: 0x1, 0xA5: 0x1, 0xA0: 0x4, 0xA1: 0x4}
    # An unassigned key, sent while Win is held so releasing Win doesn't open Start.
    VK_MASK = 0xE8

    def __init__(self, hotkeys: dict, on_error):
        """``hotkeys``: {"win+c": callback, ...}. Raises ValueError for an unknown hotkey."""
        super().__init__(daemon=True)
        self.keys = {parse_hotkey(text): callback for text, callback in hotkeys.items() if text}
        self.hotkey = ", ".join(hotkeys)
        self.on_error = on_error
        self.thread_id = None
        self.ready = threading.Event()
        self._swallowing = None  # vk of our key while it is down: swallow its repeats and release
        self._proc = None  # keep the callback alive

    def _modifiers(self) -> int:
        """Modifiers held right now (asked from Windows, so it can't go stale)."""
        flags = 0
        for vk, flag in self.MODIFIER_KEYS.items():
            if user32.GetAsyncKeyState(vk) & 0x8000:
                flags |= flag
        return flags

    def _handle(self, n_code, w_param, l_param):
        if n_code == 0:
            info = ctypes.cast(l_param, ctypes.POINTER(_KbdHook)).contents
            # Our own injected keys (Ctrl+C, the mask key) pass through untouched.
            if not info.flags & self.LLKHF_INJECTED:
                down = w_param in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN)
                vk = info.vkCode
                if self._swallowing == vk:
                    if not down:
                        self._swallowing = None
                    return 1
                if down:
                    mods = self._modifiers()
                    callback = self.keys.get((mods, vk))
                    if callback is not None:
                        self._swallowing = vk
                        if mods & 0x8:
                            user32.keybd_event(self.VK_MASK, 0, 0, 0)
                            user32.keybd_event(self.VK_MASK, 0, KEYEVENTF_KEYUP, 0)
                        # Work happens elsewhere: the hook must return fast.
                        threading.Thread(target=callback, daemon=True).start()
                        return 1
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def run(self) -> None:
        self.thread_id = kernel32.GetCurrentThreadId()
        self._proc = _HookProc(self._handle)
        hook = user32.SetWindowsHookExW(self.WH_KEYBOARD_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
        self.ready.set()
        if not hook:
            self.on_error(f"Could not watch the keyboard for {self.hotkey} "
                          f"(error {ctypes.get_last_error()}).")
            return
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass
        finally:
            user32.UnhookWindowsHookEx(hook)

    def stop(self) -> None:
        self.ready.wait(2)
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, self.WM_QUIT, 0, 0)


if IS_WINDOWS:
    class _KbdHook(ctypes.Structure):
        _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    _HookProc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HookProc, ctypes.c_void_p, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p


# --- Selection (Windows) ---------------------------------------------------------

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x2
VK_CONTROL, VK_C = 0x11, 0x43
KEYEVENTF_KEYUP = 0x2
# Keys that must be released before we send Ctrl+C (else e.g. Win+Ctrl+C).
_HELD_KEYS = (0x5B, 0x5C, 0x10, 0x11, 0x12)  # LWin, RWin, Shift, Ctrl, Alt

if IS_WINDOWS:
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]


def _open_clipboard() -> bool:
    for _ in range(20):  # another app may hold it for a moment
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def get_clipboard_text() -> str | None:
    if not _open_clipboard():
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> None:
    data = text.encode("utf-16-le") + b"\0\0"
    if not _open_clipboard():
        return
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        pointer = kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        user32.SetClipboardData(CF_UNICODETEXT, handle)
    finally:
        user32.CloseClipboard()


def copy_selection() -> str:
    """Copy the selected text of the foreground app; leaves the clipboard as it was."""
    deadline = time.monotonic() + 1.5
    while any(user32.GetAsyncKeyState(k) & 0x8000 for k in _HELD_KEYS) and time.monotonic() < deadline:
        time.sleep(0.02)
    before = user32.GetClipboardSequenceNumber()
    previous = get_clipboard_text()
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_C, 0, 0, 0)
    user32.keybd_event(VK_C, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
    deadline = time.monotonic() + 0.6
    while user32.GetClipboardSequenceNumber() == before and time.monotonic() < deadline:
        time.sleep(0.02)
    if user32.GetClipboardSequenceNumber() == before:
        return ""  # nothing selected (or the app ignored Ctrl+C)
    selection = get_clipboard_text() or ""
    if previous is not None:
        set_clipboard_text(previous)
    return selection


def capture_snip(timeout: float = 60) -> bytes | None:
    """Open Windows' snipping overlay and return the snip as PNG (None if cancelled)."""
    if not IS_WINDOWS:
        return None
    import io

    from PIL import Image, ImageGrab

    before = user32.GetClipboardSequenceNumber()
    os.startfile("ms-screenclip:")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.25)
        if user32.GetClipboardSequenceNumber() == before:
            continue
        time.sleep(0.2)  # let the snipping tool finish writing
        image = ImageGrab.grabclipboard()
        if isinstance(image, Image.Image):
            image = image.convert("RGB")
            image.thumbnail((1920, 1920))  # bounds the number of image tokens
            buf = io.BytesIO()
            image.save(buf, "PNG")
            return buf.getvalue()
        before = user32.GetClipboardSequenceNumber()
    return None


# --- Windows integration ----------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command() -> str:
    if getattr(sys, "frozen", False):  # built with PyInstaller
        return f'"{sys.executable}"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else sys.executable}" "{Path(__file__).resolve()}"'


def set_autostart(enabled: bool) -> None:
    if not IS_WINDOWS:
        return
    # CreateKeyEx also opens it; the Run key is missing on some fresh profiles.
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP)
            except FileNotFoundError:
                pass


def already_running() -> bool:
    if not IS_WINDOWS:
        return False
    kernel32.CreateMutexW(None, False, f"Local\\{APP}")
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


# --- Sending ------------------------------------------------------------------

def compose(selection: str, note: str) -> str:
    """Text for the API: the note first (it can say what is meant), then the selection."""
    selection, note = selection.strip(), note.strip()
    if note and selection:
        return f"My note: {note}\n\nText:\n{selection}"
    return note or selection


def check_connection(url: str, key: str) -> str:
    """One-line verdict for the settings dialog."""
    status, body = calsend.request("GET", url.rstrip("/") + "/jobs/connection-test", key)
    if status == 404:
        return "OK: server reached, API key accepted."
    if status == 401:
        return "Server reached, but the API key was refused."
    return f"Unexpected answer: HTTP {status}"


# --- UI ---------------------------------------------------------------------------

def tray_image():
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 8, 60, 60), 8, fill=(37, 99, 235), outline=(255, 255, 255), width=2)
    d.rectangle((4, 8, 60, 22), fill=(29, 78, 216))
    for x in (18, 46):
        d.rectangle((x - 3, 2, x + 3, 14), fill=(255, 255, 255))
    d.line((20, 40, 29, 49, 45, 31), fill=(255, 255, 255), width=6)
    return img


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.withdraw()
        root.title(APP)
        self.settings = load_settings()
        self.events: queue.Queue = queue.Queue()  # (callable, args) to run on the Tk thread
        self.popup_window = None
        self.settings_window = None
        self.hotkey_thread = None
        self.icon = None
        self.queue = localgpu.DiskQueue(CONFIG_DIR / "queue")
        self.worker = None
        root.after(100, self._drain)
        root.after(5000, self._refresh_menu)

    # -- thread plumbing: other threads never touch Tk directly
    def later(self, func, *args) -> None:
        self.events.put((func, args))

    def _drain(self) -> None:
        while True:
            try:
                func, args = self.events.get_nowait()
            except queue.Empty:
                break
            func(*args)
        self.root.after(100, self._drain)

    def notify(self, message: str, title: str = APP) -> None:
        if self.icon is not None:
            try:
                self.icon.notify(message[:250], title)
                return
            except Exception:
                pass
        print(f"{title}: {message}")

    # -- tray and hotkey
    def start_tray(self) -> None:
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem("New entry", lambda: self.later(self.open_popup, ""), default=True),
            pystray.MenuItem("Screenshot…", lambda: threading.Thread(target=self._on_screenshot, daemon=True).start()),
            pystray.MenuItem(lambda _item: self.status_line(), None, enabled=False),
            pystray.MenuItem("Settings…", lambda: self.later(self.open_settings)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.later(self.quit)),
        )
        self.icon = pystray.Icon(APP, tray_image(), "Calendar: select text, press " + self.settings["hotkey"], menu)
        self.icon.run_detached()

    def status_line(self) -> str:
        waiting = len(self.queue)
        state = self.worker.status if self.worker else "local GPU off"
        return f"Queue: {waiting} waiting · {state}"

    def _refresh_menu(self) -> None:
        if self.icon is not None:
            try:
                self.icon.update_menu()
            except Exception:
                pass
        self.root.after(5000, self._refresh_menu)

    def start_hotkey(self) -> None:
        if not IS_WINDOWS:
            return
        if self.hotkey_thread is not None:
            self.hotkey_thread.stop()
        hotkeys = {self.settings["hotkey"]: self._on_hotkey,
                   self.settings.get("screenshot_hotkey", ""): self._on_screenshot}
        try:
            self.hotkey_thread = HotkeyThread(hotkeys, lambda m: self.later(self.notify, m, "Hotkey not available"))
        except ValueError as e:
            self.notify(f"Hotkey: {e}", "Hotkey not available")
            return
        self.hotkey_thread.start()
        if self.icon is not None:
            self.icon.title = "Calendar: select text, press " + self.settings["hotkey"]

    def _on_hotkey(self) -> None:  # hotkey thread
        selection = copy_selection()
        self.later(self.open_popup, selection)

    def _on_screenshot(self) -> None:  # hotkey thread
        if self.worker is None:
            self.later(self.notify, "Screenshots need local GPU processing: turn it on in Settings.", "Not available")
            return
        png = capture_snip()
        if png:
            self.later(self.open_popup, "", png)

    # -- local GPU worker
    def start_worker(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.worker = None
        s = self.settings
        if not s.get("local_enabled"):
            return
        model = localgpu.LocalModel(s["llama_server"], s["model_file"], s.get("mmproj_file") or None)
        monitor = localgpu.GpuMonitor(load_threshold=int(s.get("gpu_load_threshold") or 30))
        server = localgpu.Server(send_text=self._server_text, send_events=self._server_events,
                                 next_review=self._next_review, review_result=self._review_result)
        self.worker = localgpu.Worker(self.queue, monitor, model, server,
                                      lambda m, t=APP: self.later(self.notify, m, t),
                                      timezone_name=s.get("timezone") or None,
                                      model_name=Path(s["model_file"]).stem or "the desktop model")
        self.worker.offer_compute = bool(s.get("offer_compute") and s.get("helper_key"))
        self.worker.start()

    def _server_text(self, text: str, received_at: str) -> str:
        code, job = calsend.send(self.settings["url"], self.settings["key"], text, None)
        return job.get("summary") or job.get("status") or "done"

    def _server_events(self, events: list, received_at: str, text: str, by: str) -> str:
        body = {"events": events, "received_at": received_at, "text": text[:20_000], "extracted_by": by}
        status, job = calsend.request("POST", self.settings["url"].rstrip("/") + "/events", self.settings["key"], body)
        if status != 200:
            raise RuntimeError(f"server answered HTTP {status}: {job.get('detail') if isinstance(job, dict) else ''}")
        return job.get("summary") or "done"

    def _next_review(self):
        if not (self.settings.get("offer_compute") and self.settings.get("helper_key")):
            return None
        status, task = calsend.request("GET", self.settings["url"].rstrip("/") + "/reviews/next?wait=0",
                                       self.settings["helper_key"])
        return task if status == 200 and task.get("id") else None

    def _review_result(self, review_id: str, answer: dict) -> None:
        calsend.request("POST", f"{self.settings['url'].rstrip('/')}/reviews/{review_id}/result",
                        self.settings["helper_key"], answer)

    def quit(self) -> None:
        if self.worker is not None:
            self.worker.stop()
        if self.hotkey_thread is not None:
            self.hotkey_thread.stop()
        if self.icon is not None:
            self.icon.stop()
        self.root.destroy()

    # -- popup: one bar with a note field, ✗ and ✓
    POPUP = {"frame": "#1c2430", "field": "#46555c", "text": "#e6e8eb", "hint": "#a9b1b8",
             "no": "#d9604f", "yes": "#8cc47a", "mark": "#1c2430"}

    def open_popup(self, selection: str, image: bytes | None = None) -> None:
        if not self.settings["url"] or not self.settings["key"]:
            self.notify("Set the server and API key first.", "Not configured")
            self.open_settings()
            return
        if self.popup_window is not None:
            self.popup_window.destroy()
        c = self.POPUP
        win = self.popup_window = tk.Toplevel(self.root, bg=c["frame"], padx=6, pady=6)
        win.overrideredirect(True)  # no title bar
        win.attributes("-topmost", True)

        hint = "Notes" if selection.strip() or image else "Appointment"
        note = tk.Entry(win, width=24, font=("Segoe UI", 20), bg=c["field"], fg=c["hint"],
                        insertbackground=c["text"], relief="flat", highlightthickness=0)
        note.insert(0, hint)
        note.grid(row=0, column=0, sticky="nsew", ipady=6, padx=(0, 6))

        def clear_hint(_event=None):
            if note.cget("fg") == c["hint"]:
                note.delete(0, "end")
                note.configure(fg=c["text"])

        def text_of_note() -> str:
            return "" if note.cget("fg") == c["hint"] else note.get()

        def send(_event=None):
            note_text = text_of_note().strip()
            if image is None and not compose(selection, note_text):
                return "break"
            self.close_popup()
            threading.Thread(target=self._dispatch, args=(selection, note_text, image), daemon=True).start()
            return "break"

        def button(column, label, color, command):
            b = tk.Label(win, text=label, font=("Segoe UI", 20, "bold"), width=3, bg=color, fg=c["mark"],
                         cursor="hand2")
            b.grid(row=0, column=column, sticky="ns", padx=(0, 6) if column == 1 else 0)
            b.bind("<Button-1>", lambda _e: command())

        button(1, "✗", c["no"], self.close_popup)
        button(2, "✓", c["yes"], send)
        note.bind("<Key>", lambda e: None if e.keysym in ("Return", "Escape", "Tab") else clear_hint(), add="+")
        note.bind("<Button-1>", clear_hint, add="+")
        note.bind("<Return>", send)
        win.bind("<Escape>", lambda _e: self.close_popup())

        win.update_idletasks()
        x, y = self.root.winfo_pointerxy()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = max(0, min(x - w // 2, win.winfo_screenwidth() - w - 8))
        y = max(0, min(y + 16, win.winfo_screenheight() - h - 48))
        win.geometry(f"+{x}+{y}")
        win.deiconify()
        win.lift()
        win.focus_force()
        note.focus_set()
        note.icursor(0)

    def close_popup(self) -> None:
        if self.popup_window is not None:
            self.popup_window.destroy()
            self.popup_window = None

    def _dispatch(self, selection: str, note: str, image: bytes | None) -> None:  # background thread
        """Decision tree: screenshots always go to the local queue; text goes local if the GPU is free."""
        if image is not None:
            self.queue.add("image", note=note, image_png=image, timezone_name=self.settings.get("timezone"))
            free = self.worker is not None and self.worker.gpu_free()
            self.later(self.notify, "Processing the screenshot on this PC…" if free else
                       "Screenshot queued; it is processed when the GPU is free.", "Queued")
            if self.worker is not None:
                self.worker.wake()
            return
        if self.worker is not None and self.worker.gpu_free():
            self.queue.add("text", text=selection.strip(), note=note, timezone_name=self.settings.get("timezone"))
            self.worker.wake()
            self.later(self.notify, "Processing on this PC…", APP)
            return
        self._send(compose(selection, note), self.settings.get("source") or None)

    def _send(self, text: str, source: str | None) -> None:  # worker thread
        self.later(self.notify, "Sending… (about 20–40 s)", APP)
        try:
            code, job = calsend.send(self.settings["url"], self.settings["key"], text, source)
            summary = job.get("summary") or job.get("status") or "done"
            title = {0: "Added to calendar", 2: "Partly added"}.get(code, "Nothing added")
        except calsend.SendError as e:
            summary, title = str(e), "Sending failed"
        self.later(self.notify, summary, title)

    # -- settings
    def open_settings(self) -> None:
        if self.settings_window is not None:
            self.settings_window.lift()
            self.settings_window.focus_force()
            return
        win = self.settings_window = tk.Toplevel(self.root)
        win.title(f"{APP} settings")
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=10)
        frame.pack(fill="both", expand=True)

        url = tk.StringVar(value=self.settings["url"])
        key = tk.StringVar(value=self.settings["key"])
        hotkey = tk.StringVar(value=self.settings["hotkey"])
        source = tk.StringVar(value=self.settings.get("source", ""))
        autostart = tk.BooleanVar(value=self.settings.get("autostart", False))
        local = {k: tk.StringVar(value=str(self.settings.get(k, ""))) for k in
                 ("screenshot_hotkey", "llama_server", "model_file", "mmproj_file", "timezone",
                  "gpu_load_threshold", "helper_key")}
        local_enabled = tk.BooleanVar(value=self.settings.get("local_enabled", False))
        offer_compute = tk.BooleanVar(value=self.settings.get("offer_compute", False))
        status = tk.StringVar()

        rows = [
            ("Server", ttk.Entry(frame, textvariable=url, width=42)),
            ("API key", ttk.Entry(frame, textvariable=key, width=42, show="•")),
            ("Hotkey", ttk.Entry(frame, textvariable=hotkey, width=20)),
            ("Default source", ttk.Combobox(frame, textvariable=source, values=SOURCES, width=17, state="readonly")),
        ]
        for i, (label, widget) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky="w", pady=3, padx=(0, 8))
            widget.grid(row=i, column=1, sticky="w", pady=3)
        ttk.Label(frame, text="e.g. https://ingest.example.com  ·  hotkey e.g. win+c, ctrl+alt+c",
                  foreground="gray").grid(row=4, column=1, sticky="w")
        ttk.Checkbutton(frame, text="Start with Windows", variable=autostart).grid(
            row=5, column=1, sticky="w", pady=(6, 0))

        gpu = ttk.LabelFrame(frame, text="Local GPU", padding=8)
        gpu.grid(row=6, column=0, columnspan=2, sticky="we", pady=(10, 0))
        ttk.Checkbutton(gpu, text="Process on this PC's GPU when it is free (needed for screenshots)",
                        variable=local_enabled).grid(row=0, column=0, columnspan=3, sticky="w")

        def browse(var, title, types):
            from tkinter import filedialog

            path = filedialog.askopenfilename(parent=win, title=title, filetypes=types)
            if path:
                var.set(path)

        gpu_rows = [
            ("llama-server.exe", "llama_server", [("llama-server", "llama-server*"), ("All", "*")]),
            ("Model (.gguf)", "model_file", [("GGUF", "*.gguf")]),
            ("Vision projector", "mmproj_file", [("GGUF", "mmproj*.gguf"), ("GGUF", "*.gguf")]),
        ]
        for i, (label, key_name, types) in enumerate(gpu_rows, start=1):
            ttk.Label(gpu, text=label).grid(row=i, column=0, sticky="w", pady=2, padx=(0, 8))
            ttk.Entry(gpu, textvariable=local[key_name], width=36).grid(row=i, column=1, sticky="w", pady=2)
            ttk.Button(gpu, text="…", width=3,
                       command=lambda v=local[key_name], t=label, ty=types: browse(v, t, ty)).grid(row=i, column=2)
        small = [("Screenshot hotkey", "screenshot_hotkey"), ("Time zone", "timezone"),
                 ("Busy above GPU load %", "gpu_load_threshold")]
        for i, (label, key_name) in enumerate(small, start=4):
            ttk.Label(gpu, text=label).grid(row=i, column=0, sticky="w", pady=2, padx=(0, 8))
            ttk.Entry(gpu, textvariable=local[key_name], width=20).grid(row=i, column=1, sticky="w", pady=2)
        ttk.Checkbutton(gpu, text="Offer compute: re-check the server's uncertain events when idle",
                        variable=offer_compute).grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(gpu, text="Helper key").grid(row=8, column=0, sticky="w", pady=2, padx=(0, 8))
        ttk.Entry(gpu, textvariable=local["helper_key"], width=36, show="•").grid(row=8, column=1, sticky="w")

        ttk.Label(frame, textvariable=status, wraplength=420).grid(row=7, column=0, columnspan=2, sticky="w",
                                                                    pady=(8, 0))

        def test():
            status.set("Testing…")

            def run():
                try:
                    verdict = check_connection(url.get().strip(), key.get().strip())
                except Exception as e:  # network errors, bad URL
                    verdict = f"Could not reach the server: {e}"
                self.later(status.set, verdict)

            threading.Thread(target=run, daemon=True).start()

        def save():
            try:
                parse_hotkey(hotkey.get())
                if local["screenshot_hotkey"].get().strip():
                    parse_hotkey(local["screenshot_hotkey"].get())
            except ValueError as e:
                status.set(f"Hotkey: {e}")
                return
            try:
                threshold = int(local["gpu_load_threshold"].get() or 30)
                ZoneInfo(local["timezone"].get().strip() or "UTC")
            except (ValueError, KeyError) as e:
                status.set(f"Check the GPU load % and time zone ({e})")
                return
            if local_enabled.get():
                missing = [n for n in ("llama_server", "model_file") if not Path(local[n].get().strip()).is_file()]
                if missing:
                    status.set("Local GPU: file not found: " + ", ".join(missing))
                    return
            old_keys = (self.settings["hotkey"], self.settings.get("screenshot_hotkey"))
            self.settings.update(url=url.get().strip(), key=key.get().strip(), hotkey=hotkey.get().strip().lower(),
                                 source=source.get(), autostart=autostart.get(),
                                 local_enabled=local_enabled.get(), offer_compute=offer_compute.get(),
                                 gpu_load_threshold=threshold,
                                 **{k: local[k].get().strip() for k in ("screenshot_hotkey", "llama_server",
                                                                        "model_file", "mmproj_file", "timezone",
                                                                        "helper_key")})
            self.settings["screenshot_hotkey"] = self.settings["screenshot_hotkey"].lower()
            save_settings(self.settings)
            self.start_worker()
            try:
                set_autostart(self.settings["autostart"])
            except OSError as e:
                self.notify(f"Could not change autostart: {e}")
            if (self.settings["hotkey"], self.settings.get("screenshot_hotkey")) != old_keys:
                self.start_hotkey()
            close()

        def close():
            win.destroy()
            self.settings_window = None

        buttons = ttk.Frame(frame)
        buttons.grid(row=8, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Test connection", command=test).pack(side="left")
        ttk.Button(buttons, text="Save", command=save).pack(side="left", padx=6)
        ttk.Button(buttons, text="Cancel", command=close).pack(side="left")
        win.protocol("WM_DELETE_WINDOW", close)
        win.attributes("-topmost", True)
        win.focus_force()


def main() -> None:
    if already_running():
        return
    if IS_WINDOWS:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on scaled displays
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    app = App(root)
    app.start_tray()
    app.start_hotkey()
    try:
        app.start_worker()
    except Exception as e:  # e.g. model files moved: keep the rest working
        app.notify(f"Local GPU processing is off: {e}", "Local GPU")
    if not app.settings["url"] or not app.settings["key"]:
        app.later(app.open_settings)
    root.mainloop()


if __name__ == "__main__":
    main()
