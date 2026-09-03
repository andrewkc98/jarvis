# Threat Model — Local JARVIS HUD

Status: current-state model, updated 2026-09-03. This document describes the controls
implemented by the local JARVIS voice, CLI, and browser/HUD paths. It is a security
model for this single-user, local device; it is not a general recommendation for a
shared or higher-stakes machine.

## 1. Scope

JARVIS has a push-to-talk voice path (audio capture, local speech-to-text, Claude-backed
response, and Piper text-to-speech), a CLI process, and a local FastAPI service used by
the browser/HUD. The CLI process and the browser/HUD process are separate processes.
Each process owns its own `JarvisService` and SDK client, and they do not share Claude
conversation history. API command turns are serialized within the API process so its
SDK client is not used concurrently; this does not merge the two processes or serialize
CLI turns with HUD turns.

The browser/HUD command path can invoke the same model and tool path as the CLI. Both
paths use the same seven-tool confirmation gate and the same accepted
`bypassPermissions` posture: the hook explicitly gates the seven mutating tools, while
other tools retain the full-capability posture accepted for this single-user device.

The service binds to loopback through the supported launcher. The system uses direct
macOS Calendar access and the Obsidian Local REST API/native MCP endpoint. The security
of those external components and macOS's own permission system is out of scope; this
document covers how this repository handles their data and credentials.

## 2. Assets

- **Vault contents** — personal notes, potentially including financial, medical, and
  security-research material.
- **Calendar data** — schedule, meeting titles and descriptions, and attendee details.
- **Voice audio** — anything audible during a push-to-talk capture window.
- **System state** — the vitals exposed by the local API.
- **Telemetry metadata** — a local `0600` record of schema version, UTC timestamp,
  route/path, durations, fired tool names, and exception class name. It contains no
  prompt, response, raw error message, or vault content.
- **Pending approval record** — the local `0600` `pending_approval.json` record,
  including the sanitized action description and the current decision. It is a
  protected sensitive asset shared by the local confirmation channels.
- **MCP/service credentials** — the Obsidian REST token and the generated
  credential-bearing MCP configuration. The token is used by the vault provider and
  as a bearer credential for the loopback Obsidian MCP endpoint. The generated
  configuration is `0600` and excluded from git. There is no CalDAV credential: macOS
  Calendar access is controlled by an OS permission grant.

## 3. Actors and trust boundaries

- **The human/device owner** — the only intended initiator of voice capture, and the
  person who authorizes a guarded tool action. Trusted to operate this single-user
  device.
- **Claude (Agent SDK and MCP)** — trusted to be useful, but not trusted to distinguish
  instructions from data. The model is not a security boundary; the confirmation hook,
  local approval record, process boundary, and OS permissions provide the surrounding
  controls.
- **Vault notes, calendar data, and retrieved MCP results** — data, not instructions.
  They may contain pasted web text, forwarded messages, or other adversarial content.
- **Anyone physically nearby** — may hear audio or TTS and may influence speech during
  an open capture window, but has no intended network access to the service.
- **Local processes and local network clients** — loopback access is part of the local
  trust boundary. The supported launcher binds the service to `127.0.0.1`; changing
  that binding or adding authentication would require a fresh review.

## 4. Inputs that reach the agent

| Input | Trust level | Notes |
|---|---|---|
| Voice transcript | Trusted origin, unreliable content | Push-to-talk limits who starts capture, but STT can mistranscribe. Ambiguous requests should be clarified rather than guessed. |
| Vault notes and query results | Trusted source, untrusted content | Treat all retrieved text as data, never as higher-priority instructions. |
| Calendar event titles/descriptions | Trusted source, untrusted content | Invitations can contain text from other people and are a plausible injection vector. |
| System vitals | Trusted structured data | Local numeric/system data is not normally attacker-shaped text. |
| Browser/HUD `/command` text | Local user input, untrusted content | It reaches the API process's separate SDK-owning `JarvisService` and the same confirmation path as the CLI. |

## 5. Capabilities and state changes

| Surface | Capability | State-changing? |
|---|---|---|
| Voice and CLI | Model responses, direct provider reads, and SDK/MCP tools | Potentially yes; guarded mutating tools require confirmation. |
| Browser/HUD `/command` | Model responses through the API process's SDK-owning service | Potentially yes; it uses the same seven-tool gate and approval record as the CLI. |
| Browser/HUD read routes | Vitals, schedule, vault summary, and telemetry metadata | No direct state change. |
| TTS | Plays the model response locally | No repository state change, but people nearby may hear sensitive content. |

The seven guarded tools are `vault_append`, `vault_copy`, `vault_delete`, `vault_move`,
`vault_patch`, `vault_write`, and `command_execute` under their qualified
`mcp__obsidian__` names. The hook is narrow and explicit. Because the SDK runs with
`bypassPermissions`, tools outside this set retain the accepted full-capability posture;
the hook is not a general allow-list or default-deny boundary.

## 6. Confirmation and approval record

Before a guarded tool executes, the confirmation hook presents a sanitized preview to
the human. The preview is the approval record's `description`; it is not raw tool input
and must not be treated as an unrestricted copy of the tool request.

Confirmation is dual-channel. Either the terminal or the HUD can submit `allow` or
`deny`. When a pending slot exists, both channels contend through the shared
`pending_approval.json` record. The record atomically accepts the first valid decision;
later answers cannot overwrite or reverse the winner. The winning decision and source
are read back from that persisted record. A terminal-only confirmation is used only
when no HUD record was claimed. The record is local, `0600`, and should be treated as a
protected sensitive asset.

The `JARVIS_SKIP_CONFIRMATION` environment variable can disable this narrow gate when
the human deliberately needs it disabled. The bypass is visible rather than silent.
All other tools continue under the accepted full-capability `bypassPermissions` posture.

## 7. Attack and failure scenarios

1. **Indirect prompt injection via a vault note.** A note may say to ignore prior
   instructions and delete notes. Retrieved text is data and the model may still be
   misled. A resulting guarded write must pass through the confirmation hook and show
   its sanitized description to the human; either terminal or HUD can decide, and the
   shared record makes the first valid decision authoritative. This does not protect
   tools outside the seven-tool set, which retain the accepted full-capability posture,
   nor does it help if the human enables `JARVIS_SKIP_CONFIRMATION`. Reconsider this
   accepted risk if the device becomes shared or the vault's content hygiene changes.
2. **Indirect prompt injection via a calendar invite.** Event text is untrusted data and
   may influence a response or a proposed action. Guarded writes still require the
   confirmation flow. Calendar access itself is read-only in this integration and is
   protected by macOS's Calendar permission.
3. **STT mistranscription triggering the wrong action.** A mistranscribed request may
   name the wrong note or a broader operation. The confirmation preview gives the human
   an opportunity to deny a guarded action, but it cannot make speech recognition
   reliable and does not cover non-guarded tools.
4. **Telemetry as a secondary data-exposure surface.** Telemetry is deliberately
   metadata-only: schema version, UTC timestamp, route/path, durations, fired tool names,
   and exception class name. It stores no prompt, response, raw error message, or vault
   content. The local file is `0600`; access to the device remains the primary protection.
5. **Open mic capturing more than the intended command.** Push-to-talk bounds capture
   to the keypress window, but anything said during that window can be recorded for
   transcription. Raw audio should remain in memory for STT and be discarded afterward,
   unless the user explicitly enables a separate debug-save-audio mode. TTS can also be
   overheard by people nearby.
6. **Local API exposure.** The supported launcher binds Uvicorn to `127.0.0.1` and the
   API is not intended to be a network service. `/command` is an active model/tool entry
   point, not merely a read endpoint. CORS is restrictive by default and only permits a
   configured HUD origin. Running the app with a different bind address or exposing it
   through a proxy changes the threat model.
7. **Credential exposure.** The Obsidian REST token grants vault access and authenticates
   the loopback MCP endpoint with a bearer header. The generated MCP configuration is a
   credential-bearing artifact, so its `0600` permissions and gitignore entry are
   load-bearing controls. The endpoint remains loopback-only. Calendar access uses an
   OS permission rather than a stored CalDAV secret.
8. **Work-machine policy.** Vault write access and a continuously available local
   assistant may not be appropriate under every institute or employer device policy.
   This is an operational risk, not a technical control; verify the applicable policy
   before enabling unattended/background operation.

## 8. Current verification and assumptions

- The Obsidian Local REST API/native MCP endpoint is loopback-only and authenticated by
  the configured bearer token.
- The service's supported launcher binds to loopback, and the browser/HUD command path
  uses a separate process-local SDK-owning service from the CLI.
- API command turns are serialized within the API process. The two processes do not
  share conversation history or a single SDK client.
- The seven-tool confirmation hook is active for both channels, and the shared local
  approval record provides first-writer-wins arbitration.
- Telemetry is local `0600` metadata only, with no prompt, response, raw error message,
  or vault content.
- The human's acceptance of full-capability `bypassPermissions` outside the narrow
  seven-tool gate applies to this single-user device and should be revisited if its
  ownership, exposure, or data sensitivity changes.

## 9. Re-review triggers

Revisit this document rather than appending a history note when any of the following
changes:

- the API bind address, CORS policy, authentication, or process ownership model;
- the seven-tool confirmation set, approval-file format, or bypass behavior;
- a new MCP server, external data source, or schedule write capability;
- telemetry fields, file permissions, or retention behavior;
- vault-content practices, device ownership, or whether the assistant runs unattended;
- voice activation, raw-audio persistence, or TTS routing.
