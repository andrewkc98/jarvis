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
