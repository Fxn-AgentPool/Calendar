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

## Anything else

Any tool that can send an HTTP POST works (Tasker, n8n, Home Assistant, a mail filter
piping into `calsend.py`, …). Keep the API key out of logs and shared configs; if it
leaks, create a new one with `scripts/new_api_key.sh` and update `.env`.
