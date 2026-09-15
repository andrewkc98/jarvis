# Jarvis

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
brew install ffmpeg
pip install --upgrade pip
pip install -e ".[dev]"
```

## Configuration

- Set `OBSIDIAN_REST_TOKEN` before running anything that talks to Claude or the vault. It may be provided as an environment variable or through macOS Keychain using the `keyring` mechanism used by `config.get_credential()`.
- In Obsidian, install the Obsidian Local REST API plugin (version 5.1.0 or later) and enable its native MCP endpoint loopback listener at `http://127.0.0.1:27123/mcp/`. This is a one-time setting in the plugin; this codebase does not configure it.
- `mcp-config.json` is generated at process start from the committed `mcp-config.json.example` template. It is gitignored, written with `0600` permissions, and should never be hand-edited or committed.
- Optionally set `OBSIDIAN_REST_BASE_URL` to override the default `https://localhost:27124`.
- `JARVIS_API_PORT` — port the local data API listens on. Defaults to `8765`. Must be a valid integer; if it is not, the process fails to start with a clear error message.
- `HUD_ORIGIN` — the origin (scheme + host + port, e.g. `http://localhost:3000`) of the HUD web frontend that is allowed to call the API cross-origin. Unset by default; when unset the API has no cross-origin access at all. Set it to the HUD's actual origin to enable cross-origin requests.
- `JARVIS_VOICE_MODEL` — path to the Piper `.onnx` voice model the API process uses for `/command`. Unset by default; `/command` returns `503` with `{"error": "voice_model_not_configured"}` until it is set.

For more detail, see [`docs/mcp-inventory.md`](docs/mcp-inventory.md) and [`docs/threat-model.md`](docs/threat-model.md).

## Usage

Run one interaction:

    python -m jarvis.orchestrator.cli --voice-model <path-to-piper-voice.onnx> --text "what's on my schedule"

Omit `--text` to use push-to-talk capture instead (press Enter to start recording, Enter
again to stop).

Schedule and calendar requests (matched as whole words, e.g. "schedule" or "calendar")
are answered directly from macOS Calendar.app via EventKit, with no Claude or MCP
round-trip. Every other request is answered by Claude (Agent SDK), which streams its
response and speaks it sentence-by-sentence as it arrives, with full built-in tool and
MCP access to the configured Obsidian vault — see `docs/threat-model.md` for the
accepted-risk posture behind that.

`orchestrator/cli_backend.py` (the Phase 1b `claude -p` shell-out) is no longer called
automatically by any code path; it remains in the codebase as a manually invocable
reference implementation only.

The first schedule query triggers a one-time macOS Calendar permission dialog.

### Calendar access troubleshooting

To inspect the EventKit authorization state without creating an event store, prompting
for access, or reading calendar content, run this content-blind check from the repository
venv:

```bash
.venv/bin/python -c 'import EventKit; print(EventKit.EKEventStore.authorizationStatusForEntityType_(EventKit.EKEntityTypeEvent))'
```

The installed binding reports status `3` when Calendar events have readable full access;
status `0` means access is still not determined. Other statuses are not readable by
Jarvis. If access is not readable, open System Settings → Privacy & Security → Calendars
and allow the identity that will run Jarvis. Launch and check the API from the same
Terminal/application identity that was authorized: macOS TCC permissions are identity-
specific, and different Python or Terminal hosts must not be assumed to share an
authorization identity. Do not use a blanket `tccutil reset` while troubleshooting.

Seven Obsidian actions that modify the vault (`vault_append`, `vault_copy`,
`vault_delete`, `vault_move`, `vault_patch`, `vault_write`, `command_execute`) pause for
a terminal yes/no confirmation before executing, as a safeguard against speech-to-text
mistranscription. Every other action — reads, searches, and anything outside Obsidian —
is unaffected. Set `JARVIS_SKIP_CONFIRMATION=1` before starting Jarvis to skip these
confirmations for that run; a notice is still printed for each one that's auto-approved.

## Local HUD and data API

For interactive use, start the local API and HUD together:

```bash
./jarvis-hud
```

This starts the loopback API on port `8765`, serves the HUD on port `4173` by default,
supplies the API's CORS origin and voice-model path, opens one browser tab at the printed
`127.0.0.1` URL, and remains in the foreground. Press `Ctrl-C` to shut down the servers
owned by the launcher.

The launcher accepts these options:

- `--voice-model PATH` — use a specific Piper `.onnx` model. Model resolution precedence
  is the explicit option, then `JARVIS_VOICE_MODEL`, then
  `<repository>/en_US-lessac-medium.onnx`; the matching `<model>.json` companion is also
  required.
- `--hud-port PORT` — serve the HUD on a different port (default `4173`; it cannot be
  `8765`).
- `--no-browser` — start the API and HUD without opening a browser tab.

Opening `hud/index.html` directly as `file://` leaves the display in mock mode because it
has no allowed HTTP origin. Use `./jarvis-hud` for the live HUD.

For an advanced/manual API-only launch, retain the existing environment configuration
(`JARVIS_API_PORT`, `HUD_ORIGIN`, and `JARVIS_VOICE_MODEL`) and run:

```bash
python -m jarvis.api.launcher
```

A raw `uvicorn` invocation with a different host is not supported.

### Endpoints

Once the service is running, you can exercise the endpoints directly (adjust the port if
`JARVIS_API_PORT` was overridden):

```bash
curl localhost:8765/vitals
curl localhost:8765/schedule
curl localhost:8765/vault-summary
curl localhost:8765/telemetry
```

### Approval and command endpoints

Beyond the read-only data endpoints above, the API also exposes a small approval and
command surface for the HUD:

- `GET /pending-approval` — returns whatever is currently awaiting confirmation, if
  anything (the vault-mutating action that's paused on a yes/no answer), or empty when
  nothing is pending.
- `POST /allow` and `POST /deny` — body `{"id": ...}`. Together they answer a pending
  confirmation from the HUD instead of the terminal, using the `id` from
  `GET /pending-approval`.
- `POST /command` — body `{"text": ..., "speak": true|false}`. Runs a typed request
  through the same pipeline as a spoken one; `speak` controls whether the machine says
  the answer out loud (default `true`).

One known limitation: a command typed into the HUD and a voice session in the CLI are
separate conversations with no shared history — they are different processes, not two
windows onto the same session.

### Telemetry

Each completed interaction appends a line to `telemetry.jsonl` at the repository root.
This file is gitignored and retained for 30 days or 5,000 entries, whichever is reached
first.

Each entry records pipeline timing (how long each stage took) and which tools fired. It
does **not** record prompts, responses, or any vault/calendar content.

### Pending approval file

`pending_approval.json` lives at the repository root (also gitignored). Unlike
`telemetry.jsonl`, it holds at most one record at a time and is deleted once the pending
confirmation is answered — it is not a log.
