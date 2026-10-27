"""Checks the Windows-only parts of calsend_tray.py (run on Windows).

    python clients\\check_windows.py

Uses a temporary APPDATA, so your real settings are not touched. The
clipboard is overwritten with a test text.
"""

import os
import sys
import tempfile
import time
import winreg
from pathlib import Path

os.environ["APPDATA"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent))
import calsend_tray as t  # noqa: E402

# API key encryption (DPAPI) round trip, and the settings file.
stored = t.protect("secret-key")
assert stored.startswith("dpapi:") and "secret-key" not in stored
assert t.unprotect(stored) == "secret-key"
t.save_settings(dict(t.DEFAULTS, url="https://example.com", key="k123"))
assert "k123" not in t.SETTINGS_FILE.read_text()
assert t.load_settings()["key"] == "k123"
print("DPAPI + settings: ok")

# Clipboard read/write.
t.set_clipboard_text("hello calendar")
assert t.get_clipboard_text() == "hello calendar"
print("clipboard: ok")

# Autostart registry entry (added and removed again, unless it was already on).
try:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, t.RUN_KEY) as key:
        winreg.QueryValueEx(key, t.APP)
    was_on = True
except FileNotFoundError:  # no Run key, or no entry for us
    was_on = False
t.set_autostart(True)
with winreg.OpenKey(winreg.HKEY_CURRENT_USER, t.RUN_KEY) as key:
    print("autostart command:", winreg.QueryValueEx(key, t.APP)[0])
if not was_on:
    t.set_autostart(False)
print("autostart: ok")

# Keyboard hook for the hotkey: installed and removed again.
errors = []
thread = t.HotkeyThread("win+c", lambda: None, errors.append)
thread.start()
thread.ready.wait(5)
time.sleep(0.5)
thread.stop()
thread.join(5)
assert not errors, errors[0]
print("keyboard hook: installed and removed")
print("all checks passed")
