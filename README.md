# Calendar

Self-hosted calendar (Radicale/CalDAV) with a local LLM that turns forwarded
text into calendar events. Design: [docs/DESIGN.md](docs/DESIGN.md).

Status: **milestone 2**: Radicale behind Pangolin, local LLM with benchmark.

## Requirements

- Debian (or any Linux) with Docker and the Docker Compose plugin
- A Pangolin server on a VPS with this box connected as a site (Newt)

## Setup

```sh
git clone https://github.com/Fxn-AgentPool/Calendar.git && cd Calendar
cp .env.example .env        # set CAL_USER (your username) and USER_TIMEZONE
docker compose build

# Create the three accounts. Each prints a random password once:
# store them in your password manager.
scripts/radicale-passwd.sh <your CAL_USER>
scripts/radicale-passwd.sh ingest
scripts/radicale-passwd.sh agenda
# (Use `--prompt` as the second argument to type your own password instead.)

docker compose up -d
docker compose ps           # should show "healthy" after ~30 s
```

The `ingest` and `agenda` accounts are for later milestones; creating them
now lets the check below test all permissions.

Check that logins and permissions work as designed:

```sh
OWNER_PASS=... INGEST_PASS=... AGENDA_PASS=... scripts/smoke_test.sh
```

Your first login creates a calendar called **Personal**.

## Pangolin

In the Pangolin dashboard, add a resource:

| Setting | Value |
|---------|-------|
| Type | HTTP |
| Subdomain | `cal` (→ `cal.<domain>`) |
| Site | this box |
| Target | `radicale:5232` if Newt runs in Docker, `127.0.0.1:5232` if Newt runs on the host |
| Authentication | **off** (calendar apps can't do Pangolin's browser login; Radicale does the login) |

**Newt in Docker:** Newt must be on the `calendar` network to reach `radicale`:

- If Newt already runs in its own container, connect it:
  `docker network connect calendar <newt container>`.
- Or let this project run Newt: put `PANGOLIN_ENDPOINT`, `NEWT_ID` and
  `NEWT_SECRET` in `.env` and start with `docker compose --profile newt up -d`.

Then run the check through the tunnel:

```sh
OWNER_PASS=... scripts/smoke_test.sh https://cal.<domain>
```

Recommended on the VPS: enable CrowdSec (the Pangolin installer offers it), so
repeated failed logins get banned.

## LLM

The llama.cpp server runs CPU-only and is never exposed through Pangolin
(it is reachable only from the ingest service and on `127.0.0.1:8080`).

```sh
scripts/download_model.sh            # default: Qwen3 4B Instruct, Q4_K_M (~2.5 GB)
# set LLM_MODEL_FILE in .env to the printed file name
docker compose up -d llm
docker compose ps llm                # wait for "healthy" (loading takes ~10-30 s)
python3 scripts/bench_llm.py -v      # speed + extraction quality on scripts/samples/
```

The benchmark runs every sample in `scripts/samples/` (email, WhatsApp, a resolved poll,
all-day events, another time zone, an injection attempt) through the same prompt and
schema the ingest service uses and compares the result with the expected events. It
exits with 0 only if every event was found and nothing extra was invented.

**Choosing a model.** Try a second one and compare:

```sh
scripts/download_model.sh qwen2.5-7b     # or llama3.2-3b, or any https URL to a .gguf
# change LLM_MODEL_FILE in .env
docker compose up -d llm && python3 scripts/bench_llm.py --repeat 3
```

Prefer the smallest model that gets all samples right; add your own samples (copy one
of the `.json` files) for the kinds of text you send most.

| Model (Q4_K_M) | File size | RAM in use (8k context) |
|----------------|-----------|-------------------------|
| Llama 3.2 3B   | ~2.0 GB   | ~3 GB |
| Qwen3 4B       | ~2.5 GB   | ~3.5 GB |
| Qwen2.5 7B     | ~4.7 GB   | ~6 GB |

Settings in `.env`: `LLM_THREADS` (number of physical cores, default 6) and
`LLM_CTX_SIZE` (default 8192 tokens; enough for ~20,000 characters of input).

## Connecting devices

| Client | How |
|--------|-----|
| iPhone / iPad | Settings → Calendar → Accounts → Add Account → Other → Add CalDAV Account. Server `cal.<domain>`, your username and password. |
| Android | [DAVx⁵](https://www.davx5.com/): "Login with URL and user name", URL `https://cal.<domain>/`. |
| Thunderbird | New Calendar → On the Network → username, location `https://cal.<domain>/`. |

Radicale's own page at `https://cal.<domain>/.web/` can create, rename or
delete calendars and import/export `.ics` files. It has no calendar view.

## Operations

- **Change a password:** run `scripts/radicale-passwd.sh <user>` again. No restart needed.
- **Backup:** `scripts/backup.sh` writes `backups/radicale-<date>.tar.gz` and keeps the last 30. For nightly backups, add to crontab: `15 3 * * * /path/to/Calendar/scripts/backup.sh`. Copy `backups/` off the box as well.
- **Restore:** `docker compose down`, then
  `docker compose run --rm --no-deps -T --entrypoint tar radicale -C /var/lib/radicale -xzf - < backups/<file>.tar.gz`,
  then `docker compose up -d`.
- **Logs:** `docker compose logs -f radicale`.

## Files

| Path | What |
|------|------|
| `docker-compose.yml` | services (Radicale, LLM; optional Newt) |
| `radicale/` | image, config, rights template, entrypoint |
| `app/calapp/` | Python code; `ingest/llm.py` holds the extraction prompt and schema |
| `scripts/` | account, smoke test, backup, model download and benchmark helpers |
| `models/` | downloaded model files, not committed |
| `secrets/` | password hashes (`radicale-users`), not committed |
| `.env` | your settings, not committed |
