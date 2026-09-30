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
- `JARVIS_VOICE_INPUT_MODE` — one of `terminal`, `browser`, or `off` (exact, lowercase; any
  other value, including an empty one, aborts startup). Default `off` when unset; the
  `./jarvis-hud` launcher sets it for the API (`terminal` by default).
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

By default `./jarvis-hud` also starts one interactive voice loop in this same terminal as
a supervised child process. The loop runs one turn after another and repeats until you
interrupt the launcher; see [Usage](#usage) for the `Enter`-driven start/stop capture
controls. If the voice loop exits before a stop is requested, the launcher reports its
numeric status to stderr and shuts the owned API, HUD, and any remaining children down
together. Voice is supervised by the launcher (it is terminated, then killed on timeout,
on every shutdown path). A `SIGKILL` delivered to the voice child terminates it immediately
without Python cleanup; if the launcher remains alive, supervision observes the child's
numeric exit. A `SIGKILL` delivered to the launcher itself prevents launcher cleanup and can
leave surviving children without supervision. The HUD shows a
listening/processing/speaking/idle audio-state line and a short status note as a complement
to, but not always a refresh of, the terminal.

The launcher has three mutually exclusive voice-input modes:

| Mode | Flag | Terminal voice loop | API stdin | Guarded-tool approval |
| --- | --- | --- | --- | --- |
| `terminal` | none (default) | started, owns this terminal | detached (`/dev/null`) | HUD only for API turns; the loop's own turns use the terminal Enter prompt |
| `browser` | `--browser-voice` | not started | inherited | HUD, with the terminal yes/no prompt as a fallback |
| `off` | `--no-voice` | not started | inherited | HUD, with the terminal yes/no prompt as a fallback |

`--browser-voice` is explicit and suppresses terminal capture entirely; `--no-voice`
disables both voice inputs (typed commands still work). Combining the two flags is
rejected. The launcher passes the chosen mode to the API child as
`JARVIS_VOICE_INPUT_MODE`.

The launcher accepts these options:

- `--voice-model PATH` — use a specific Piper `.onnx` model. Model resolution precedence
  is the explicit option, then `JARVIS_VOICE_MODEL`, then
  `<repository>/en_US-lessac-medium.onnx`; the matching `<model>.json` companion is also
  required.
- `--hud-port PORT` — serve the HUD on a different port (default `4173`; it cannot be
  `8765`).
- `--no-browser` — start the API and HUD without opening a browser tab.
- `--browser-voice` — use the browser microphone in the HUD instead of terminal capture;
  no terminal voice loop is started. Mutually exclusive with `--no-voice`.
- `--no-voice` — run the API and HUD with neither terminal nor browser voice input.

Opening `hud/index.html` directly as `file://` leaves the display in mock mode because it
has no allowed HTTP origin. Use `./jarvis-hud` for the live HUD.

For an advanced/manual API-only launch, retain the existing environment configuration
(`JARVIS_API_PORT`, `HUD_ORIGIN`, `JARVIS_VOICE_MODEL`, and optionally
`JARVIS_VOICE_INPUT_MODE`) and run:

```bash
python -m jarvis.api.launcher
```

A raw `uvicorn` invocation with a different host is not supported.

### Terminal input and control

Who owns terminal input and guarded-tool approval depends on the launcher mode:

- `terminal` (default): the voice loop owns this terminal (see [Usage](#usage) for its
  `Enter`-driven capture controls) and answers any guarded-tool confirmation it reaches with
  the same Enter prompt. The API's stdin is detached, so a confirmation raised by an API
  turn (typed or otherwise) is surfaced on the HUD and decided there with **Approve** or
  **Deny**.
- `browser` (`--browser-voice`): no terminal voice child runs and the API inherits this
  terminal's stdin. Browser voice and typed-command approvals are decided on the HUD; if
  a confirmation is not answered there, the terminal yes/no prompt remains available as a
  fallback on this line.
- `off` (`--no-voice`): same stdin behavior as `browser`; the API's confirmations remain a
  terminal yes/no prompt as well as a HUD decision.

The terminal and HUD confirmation channels contend over the shared approval file and are not
cross-process serialized: the first valid decision wins. Do not intentionally run a terminal
voice turn and a HUD confirmation at the same time. The HUD's audio-state line and status
note are a complement to, and are not always refreshed by, the terminal.

Security guidance for the tools this assistant uses, the approval record, and the accepted
risk behind them is in [`docs/threat-model.md`](docs/threat-model.md).

### Browser voice

Start the launcher with `./jarvis-hud --browser-voice` to talk to Jarvis from the HUD page
instead of the terminal. Requirements: a Piper voice model (already required by the
launcher), `ffmpeg` on the `PATH`, and a browser that can record WebM/Opus, Ogg/Opus, or
MP4 audio with `MediaRecorder`.

Controls in the HUD audio panel:

- **Start mic** — asks the browser for microphone access only when you press it (the page
  never requests it on load, and `file://` mock mode never requests it). Recording starts
  once permission is granted.
- **Stop & Send** — ends the recording and uploads it. Recording also stops and sends
  automatically at the 30-second capture limit. Recordings over 8 MiB, or empty
  recordings, are rejected locally without uploading.
- **Cancel** — available from capture through the assistant turn. Before upload it simply
  discards the recording; during the turn it aborts the request and asks the API to cancel
  that exact turn.
- **Play reply / Stop playback** — the transcript and text reply are shown first. If the
  HUD **Speak** toggle is on, the page then requests a local Piper WAV and plays it. If the
  browser blocks autoplay, the text stays visible and **Play reply** plays it on demand;
  the same button replays the reply. **Stop playback** stops page audio.

Typed commands stay available whenever a voice turn is not in progress. While the browser
owns capture, a turn, synthesis, or playback, the typed input and Speak toggle are disabled
(Cancel and approval Allow/Deny stay operable), and mic start is disabled while a typed
command or an unrelated approval is pending. Typed and voice replies share the same HUD
redaction control: when redaction is on, the transcript, response, typed reply, and
playback controls are hidden and return when it is turned off.

Fallbacks, each leaving typed commands usable: in `terminal` mode the HUD disables Start mic
and tells you to relaunch with `--browser-voice`; with `--no-voice` it says voice input is
disabled; a missing voice model, missing `ffmpeg`, or an unsupported browser each show a
stable message. If microphone permission is denied, allow microphone access for the page in
your browser's site settings, then press **Start mic** again. Jarvis cannot read or change
the browser's permission preference.

Data handling: browser audio is sent only to the loopback API, decoded in memory by local
`ffmpeg`, transcribed by local MLX Whisper, and any spoken reply is synthesized by local
Piper and returned as a WAV to the page. Jarvis keeps this audio in memory only and adds no
audio, transcript, or reply text to telemetry or any persistent store. Jarvis makes no claim
about what the browser itself retains (for example permission history).

Cancellation, honestly: recording and page playback stop immediately. Exact-turn API
cancellation prevents any later model dispatch and discards the interrupted SDK conversation
state (the next turn starts a fresh SDK session). Decoder, speech-to-text, or Piper work
already running in a native worker may finish locally; its output is ignored. Cancel is not
an approval decision: it is neither Allow nor Deny, and a pending approval is cleared rather
than granted. Approvals during a browser voice turn appear on the HUD and are decided there
with Approve or Deny.

Conversation ownership: typed HUD commands and browser voice share the API process's single
reused SDK conversation, so they have shared history and are serialized (a second turn
returns `turn_in_progress`). Terminal and one-shot CLI voice own a separate process and
session with separate history. The launcher never runs terminal voice in browser mode, but a
manually started CLI remains outside the API's lock and can overlap with API turns.

This feature is covered by deterministic tests only; live browser, microphone, speaker,
Calendar, vault, and approval acceptance checks have not been performed.

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

Browser voice endpoints (only `/voice-capabilities` is available outside browser mode; the
others return `503 browser_voice_unavailable` unless `JARVIS_VOICE_INPUT_MODE=browser`):

- `GET /voice-capabilities` — returns `mode`, `available`, `reason`, `max_duration_seconds`,
  `max_upload_bytes`, and `accepted_media_types`. It never starts a service, spawns
  `ffmpeg`, or exposes paths. `reason` is one of `terminal_voice_active`, `voice_disabled`,
  `voice_model_not_configured`, `decoder_unavailable`, or `null`.
- `POST /voice-turn?turn_id=<uuid>` — body is the raw recorded audio with its actual
  `Content-Type` (`audio/webm`, `audio/ogg`, or `audio/mp4`, up to 8 MiB / 30 s). Returns
  `{turn_id, transcript, response}`. Fails fast with `409 turn_in_progress` if another turn
  holds the command lock; a turn is abandoned after 120 seconds (`504 voice_turn_timeout`).
- `POST /voice-cancel` — JSON `{"turn_id": "<uuid>"}`. Returns `{"cancelled": true}` after
  cleanup, or `409 no_matching_turn` for an unknown, stale, or finished ID.
- `POST /voice-speech` — JSON `{"text": "..."}` (1 to 10,000 characters). Returns a Piper
  `audio/wav` with `Cache-Control: no-store`, `409 speech_busy` if a synthesis is running, or
  `502 speech_failed`. It never plays audio on the server.

Error bodies are stable, content-free `{"error": "<code>"}` values.

One known limitation: a command typed into the HUD and a voice turn in the terminal voice loop
are separate conversations with no shared history — they are different processes, not two
windows onto the same session. (Browser voice, by contrast, shares the API's conversation
with typed commands; see "Browser voice".) Because of the shared approval record (see
"Terminal input and control"), do not run a HUD confirmation and a terminal voice turn that
would reach the same guarded action at the same time.

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
