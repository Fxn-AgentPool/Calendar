#!/usr/bin/env python3
"""Calendar quick-add for Windows: a tray app with a global hotkey.

Select text anywhere and press the hotkey (default Win+C): a small window
pops up with the selection and a notes field. ✓ (Enter) sends both to the
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
from tkinter import scrolledtext, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import calsend  # noqa: E402

APP = "CalendarSend"
IS_WINDOWS = sys.platform == "win32"
CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home() / ".config") / APP
SETTINGS_FILE = CONFIG_DIR / "settings.json"
DEFAULTS = {"url": "", "key": "", "hotkey": "win+c", "source": "", "autostart": False}
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


def load_settings() -> dict:
    settings = dict(DEFAULTS)
    try:
        settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    settings["key"] = unprotect(settings.get("key", ""))
    return settings


def save_settings(settings: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    stored = dict(settings, key=protect(settings["key"]) if settings["key"] else "")
    SETTINGS_FILE.write_text(json.dumps(stored, indent=2), encoding="utf-8")


# --- Hotkey --------------------------------------------------------------------

MODIFIERS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000


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
    """Registers a global hotkey and calls on_hotkey() when it is pressed."""

    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012

    def __init__(self, hotkey: str, on_hotkey, on_error):
        super().__init__(daemon=True)
        self.mods, self.vk = parse_hotkey(hotkey)
        self.hotkey = hotkey
        self.on_hotkey, self.on_error = on_hotkey, on_error
        self.thread_id = None
        self.ready = threading.Event()

    def run(self) -> None:
        self.thread_id = kernel32.GetCurrentThreadId()
        ok = user32.RegisterHotKey(None, 1, self.mods | MOD_NOREPEAT, self.vk)
        self.ready.set()
        if not ok:
            self.on_error(f"{self.hotkey} is already used by Windows or another app. "
                          "Pick another hotkey in Settings.")
            return
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == self.WM_HOTKEY:
                    self.on_hotkey()
        finally:
            user32.UnregisterHotKey(None, 1)

    def stop(self) -> None:
        self.ready.wait(2)
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, self.WM_QUIT, 0, 0)


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
        root.after(100, self._drain)

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
            pystray.MenuItem("Settings…", lambda: self.later(self.open_settings)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.later(self.quit)),
        )
        self.icon = pystray.Icon(APP, tray_image(), "Calendar: select text, press " + self.settings["hotkey"], menu)
        self.icon.run_detached()

    def start_hotkey(self) -> None:
        if not IS_WINDOWS:
            return
        if self.hotkey_thread is not None:
            self.hotkey_thread.stop()
        try:
            self.hotkey_thread = HotkeyThread(self.settings["hotkey"], self._on_hotkey,
                                              lambda m: self.later(self.notify, m, "Hotkey not available"))
        except ValueError as e:
            self.notify(f"Hotkey {self.settings['hotkey']!r}: {e}", "Hotkey not available")
            return
        self.hotkey_thread.start()
        if self.icon is not None:
            self.icon.title = "Calendar: select text, press " + self.settings["hotkey"]

    def _on_hotkey(self) -> None:  # hotkey thread
        selection = copy_selection()
        self.later(self.open_popup, selection)

    def quit(self) -> None:
        if self.hotkey_thread is not None:
            self.hotkey_thread.stop()
        if self.icon is not None:
            self.icon.stop()
        self.root.destroy()

    # -- popup: selection + note, ✓ / ✗
    def open_popup(self, selection: str) -> None:
        if not self.settings["url"] or not self.settings["key"]:
            self.notify("Set the server and API key first.", "Not configured")
            self.open_settings()
            return
        if self.popup_window is not None:
            self.popup_window.destroy()
        win = self.popup_window = tk.Toplevel(self.root)
        win.title("Add to calendar")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=8)
        frame.pack(fill="both", expand=True)

        if selection.strip():
            ttk.Label(frame, text="Selected text").pack(anchor="w")
            shown = scrolledtext.ScrolledText(frame, width=48, height=5, wrap="word")
            shown.insert("1.0", selection.strip())
            shown.configure(state="disabled")
            shown.pack(fill="x", pady=(0, 6))
            ttk.Label(frame, text="Note (optional)").pack(anchor="w")
        else:
            ttk.Label(frame, text="Nothing selected: type the appointment").pack(anchor="w")
        note = tk.Text(frame, width=48, height=3, wrap="word")
        note.pack(fill="x")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(6, 0))
        source = ttk.Combobox(buttons, values=SOURCES, width=10, state="readonly")
        source.set(self.settings.get("source", ""))
        source.pack(side="left")

        def send(_event=None):
            text = compose(selection, note.get("1.0", "end"))
            if not text:
                return "break"
            hint = source.get() or None  # read before the window (and its widgets) is gone
            self.close_popup()
            threading.Thread(target=self._send, args=(text, hint), daemon=True).start()
            return "break"

        def newline(_event=None):
            note.insert("insert", "\n")
            return "break"

        tk.Button(buttons, text="✗", width=4, command=self.close_popup).pack(side="right")
        tk.Button(buttons, text="✓", width=4, command=send).pack(side="right", padx=4)
        note.bind("<Return>", send)
        note.bind("<Shift-Return>", newline)
        win.bind("<Escape>", lambda _e: self.close_popup())
        win.protocol("WM_DELETE_WINDOW", self.close_popup)

        win.update_idletasks()
        x, y = self.root.winfo_pointerxy()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = max(0, min(x + 12, win.winfo_screenwidth() - w - 8))
        y = max(0, min(y + 12, win.winfo_screenheight() - h - 48))
        win.geometry(f"+{x}+{y}")
        win.deiconify()
        win.lift()
        win.focus_force()
        note.focus_set()

    def close_popup(self) -> None:
        if self.popup_window is not None:
            self.popup_window.destroy()
            self.popup_window = None

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
        ttk.Label(frame, textvariable=status, wraplength=360).grid(row=6, column=0, columnspan=2, sticky="w",
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
            except ValueError as e:
                status.set(f"Hotkey: {e}")
                return
            old_hotkey = self.settings["hotkey"]
            self.settings.update(url=url.get().strip(), key=key.get().strip(), hotkey=hotkey.get().strip().lower(),
                                 source=source.get(), autostart=autostart.get())
            save_settings(self.settings)
            try:
                set_autostart(self.settings["autostart"])
            except OSError as e:
                self.notify(f"Could not change autostart: {e}")
            if self.settings["hotkey"] != old_hotkey:
                self.start_hotkey()
            close()

        def close():
            win.destroy()
            self.settings_window = None

        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=2, sticky="e", pady=(10, 0))
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
    if not app.settings["url"] or not app.settings["key"]:
        app.later(app.open_settings)
    root.mainloop()


if __name__ == "__main__":
    main()
