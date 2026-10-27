# Sending text to the calendar

Every client does the same thing: `POST https://ingest.<domain>/ingest?wait=50` with the
header `Authorization: Bearer <API key>` and a JSON body, then shows the `summary` field of
the reply. The API key comes from `scripts/new_api_key.sh`.

```json
{"text": "the text", "received_at": "2026-10-25T09:12:00+02:00", "source": "whatsapp"}
```

- `text` is the only required field.
- `received_at` (when the message was sent) makes "tomorrow" or "Thursday" resolve
  correctly when you forward an older message. Without it, the server uses the current time.
- `source` is a hint for the model: `email`, `whatsapp`, `chat`, `poll`, `note`, …
- `wait=50` keeps the request open until the result is ready (up to 50 s). If processing
  takes longer, the reply is `202` with a `job_id`; the result is then at
  `GET /jobs/<job_id>` (same header).

Reply fields worth showing: `summary` (one line per event, e.g. `Added: Dentist, Tue 27 Oct
14:00–14:45` or `Not added (conflicts with Standup): …`) and `ok` (true when every event
was added or was already there).

## iPhone / iPad: Shortcut in the share sheet

Create it once in the **Shortcuts** app; afterwards select text anywhere (Mail, WhatsApp,
Messages, Safari), tap **Share → Add to Calendar**, and a notification shows the result.

1. **New shortcut**, name it *Add to Calendar*.
2. Open the shortcut's **details** (ⓘ): turn on **Show in Share Sheet**; under
   *Share Sheet Types* keep only **Text**.
3. At the top, set *Receive* **Text** from **Share Sheet**, and *If there's no input*:
   **Get Clipboard**. (Then running it from the home screen sends the clipboard.)
4. Add **Current Date**, then **Format Date**: Date Format **ISO 8601**, *Include ISO 8601
   Time* on.
5. Add **Get Contents of URL**:
   - URL: `https://ingest.<domain>/ingest?wait=50`
   - Method: **POST**
   - Headers: `Authorization` = `Bearer <your API key>`
   - Request Body: **JSON** with two fields
     - `text` (Text) = *Shortcut Input*
     - `received_at` (Text) = *Formatted Date*
6. Add **Get Dictionary Value**: key `summary` in *Contents of URL*.
7. Add **If** *Dictionary Value* **has any value**:
   - **Show Notification**: *Dictionary Value*
   - **Otherwise**: **Show Notification** "Still processing; check the agenda later."
   - **End If**

`wait=50` stays below the Shortcuts app's own 60-second time limit for web requests.

*WhatsApp:* long-press a message → **More…** → select messages → share (↗) gives the text
to the share sheet. Tip: duplicate the shortcut with a `source` field set to `whatsapp`
or `email` if you mostly forward one kind of text.

## Android: HTTP Shortcuts app

[HTTP Shortcuts](https://http-shortcuts.rmy.ch/) (free, open source; Play Store and
F-Droid) can receive shared text and call the API. Menu names may differ slightly between
versions.

1. **Variables** (from the menu): create a *Text* variable `shared_text` and tick
   **Allow 'Share with…'** → *Text*. Leave *Prompt* off.
2. **Create shortcut → Regular shortcut**, name *Add to Calendar*:
   - Method **POST**, URL `https://ingest.<domain>/ingest?wait=50`
   - *Request Headers*: `Authorization` = `Bearer <your API key>`
   - *Request Body*: **Custom text**, content type `application/json`:
     ```
     {"text": {{shared_text}}, "source": "share"}
     ```
     When inserting the variable, choose the option to **JSON-encode** it (adds the quotes
     and escapes line breaks); otherwise multi-line text breaks the JSON.
   - *Response Handling*: **Don't show** the raw response.
   - *Scripting → Run after successful execution*:
     ```js
     const result = JSON.parse(response.body);
     showToast(result.summary || "Still processing; check the agenda later.");
     ```
   - *Advanced → Timeout*: 60 s.
3. Select text in any app → **Share** → *Add to Calendar*.

## Computer: `clients/calsend.py`

Needs only Python 3.8+ (Linux, macOS, Windows, or Termux on Android).

```sh
mkdir -p ~/.config && cat > ~/.config/calsend.conf <<EOF
CALSEND_URL=https://ingest.<domain>
CALSEND_KEY=<your API key>
EOF
chmod 600 ~/.config/calsend.conf

clients/calsend.py "Dentist moved to Tuesday 14:00"
pbpaste | clients/calsend.py --source email        # macOS clipboard
clients/calsend.py --file chat.txt --source whatsapp
```

It waits for the result (and keeps polling if needed) and prints the summary. Exit code:
`0` everything added or already there, `2` some events not added (conflict or skipped),
`1` failed, `3` usage/configuration error (e.g. wrong key).

## Windows: tray app with a hotkey (`clients/calsend_tray.py`)

Runs in the background with a tray icon. **Select text anywhere and press Win+C**: a small
window opens next to the mouse with the selection and a note field. **✓ / Enter** sends
(Shift+Enter for a new line in the note), **✗ / Esc** cancels. With nothing selected you
can type the appointment directly. The result appears as a Windows notification
("Added: Dentist, Tue 01 Dec 14:00–14:45"). Your clipboard is left as it was.

The note goes first, then the selection, so it can say which part is meant ("only the
second date", "this is for next year").

**Tray menu:** *New entry* (also a left click), *Settings…*, *Quit*. Settings: server URL,
API key, hotkey, default source, *Start with Windows*, and *Test connection*. They are kept
in `%APPDATA%\CalendarSend\settings.json`; the API key is encrypted for your Windows user.

**Install** (Python 3.10+ from python.org, which includes tkinter):

```powershell
# option 1: run directly
pip install -r clients\requirements-windows.txt
pythonw clients\calsend_tray.py

# option 2: build a single CalendarSend.exe (in dist\)
powershell -ExecutionPolicy Bypass -File clients\build_windows.ps1
```

`python clients\check_windows.py` checks the Windows-only parts (key encryption, clipboard,
autostart entry, hotkey registration) without touching your real settings.

Start it once, fill in the settings (the window opens by itself on first start), press
*Test connection*, and tick *Start with Windows*.

**Win+C works even though Windows uses it** (Copilot or Search on Windows 11): the app
watches the keyboard with a low-level hook, like AutoHotkey, and catches its hotkey before
Windows does. The key is swallowed, and releasing Win does not open the Start menu. Only
the exact combination is taken (Win+C, not Win+Shift+C), and only while the app runs. No
registry changes are needed; if you set `DisabledHotkeys` earlier, you can remove it again
(`reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced" /v DisabledHotkeys /f`).
Other hotkeys can be set in Settings: modifiers (`win`, `ctrl`, `alt`, `shift`) plus a
letter, digit, `F1`–`F24` or `space`, joined with `+`.

Copying the selection works in apps that copy with Ctrl+C (browsers, Office, Outlook,
WhatsApp Desktop, most others).

## Linux desktop: `clients/calsend_gui.py` (temporary)

A bare window: paste text, optionally pick a source, press **Send** or Ctrl+Enter; the
result line appears below when the server is done (the window stays usable meanwhile).
Same configuration as `calsend.py` (`~/.config/calsend.conf`). Needs tkinter:

```sh
sudo apt install python3-tk
clients/calsend_gui.py
```

## Anything else

Any tool that can send an HTTP POST works (Tasker, n8n, Home Assistant, a mail filter
piping into `calsend.py`, …). Keep the API key out of logs and shared configs; if it
leaks, create a new one with `scripts/new_api_key.sh` (it replaces the old one) and run
`docker compose up -d ingest`.

## Desktop review helper (API ready, client not built yet)

Events the checker is unsure about are added marked `(?)` and queued (see
`docs/DESIGN.md` §8). A helper on the desktop can re-check them with a bigger model when
its GPU is idle. The server side is ready; the helper program itself
(`clients/review_helper.py`) is not written yet. This is the contract it will use.

**Enable it** on the server: run `scripts/new_api_key.sh helper` (prints the key, writes
its hash to `secrets/ingest.env`) and `docker compose up -d ingest`. Without it, the
endpoints below answer `404`. The helper key is separate from the ingest key; neither
works for the other's endpoints.

All requests: `Authorization: Bearer <helper key>`, base URL `https://ingest.<domain>`.

### `GET /reviews/next?wait=30`

Hands out the oldest open item and reserves it for 10 minutes (`REVIEW_LEASE_MINUTES`).
`wait` (0–60 s) keeps the request open until an item arrives. With nothing to do, the
reply is `204 No Content`.

```json
{
  "id": "Xc3…",
  "messages": [
    {"role": "system", "content": "You check calendar events extracted from text. …"},
    {"role": "user", "content": "Today: … Day table: … <text>…</text> … Extracted event: {…}"}
  ],
  "schema": {"type": "object", "properties": {"event": {…}}, "required": ["event"]}
}
```

The helper sends `messages` unchanged to its local llama.cpp server, with the schema as
the response format and thinking off:

```json
POST http://127.0.0.1:8080/v1/chat/completions
{"messages": <messages>, "temperature": 0, "max_tokens": 1024,
 "response_format": {"type": "json_schema", "json_schema": {"name": "review", "schema": <schema>}}}
```

### `POST /reviews/<id>/result`

Body: the model's JSON answer as is, `{"event": {"title": …, "all_day": …, "start": …,
"end": …, "timezone": …, "location": …, "notes": …}}`. Reply:

```json
{"outcome": "corrected", "detail": "Tue 27 Oct 14:00–14:45 -> Thu 29 Oct 14:00–14:45"}
```

| `outcome` | Meaning |
|-----------|---------|
| `confirmed` | Same time as before; `(?)` removed, event marked checked. |
| `corrected` | Different time, no conflict; the event was moved (same UID) and marked checked. |
| `kept` | The corrected time conflicts with another event; the event stays uncertain, with the suggestion in its notes. |
| `skipped` | The event was deleted or changed by you meanwhile; nothing touched. |
| `failed` | The answer was not a valid event; the event stays uncertain. |

Other status codes: `404` unknown or expired item, `409` item not reserved (already
finished, or never handed out), `503` calendar unreachable (retry later; the reservation
runs out after 10 minutes and the item is handed out again).

### `GET /reviews/stats`

Counts per state, e.g. `{"pending": 2, "confirmed": 5, "corrected": 1}`.

### What the helper program will do (planned)

- Run as a systemd user service on the Ubuntu desktop.
- Only ask for work when the GPU is idle: `nvidia-smi` shows low utilisation for a few
  minutes and enough free VRAM, and the local llama.cpp server answers. Rendering always
  wins; it never starts or stops other GPU work.
- Use whatever bigger model the local llama.cpp server runs (for 16 GB VRAM e.g. a Qwen3
  14B or Gemma 3 12B quantization), with thinking disabled.
