# Local JARVIS HUD — Build Plan

**Goal:** A voice-driven, locally-hosted assistant with a single-screen dark terminal HUD — system vitals, command deck, schedule, audio I/O, live Obsidian vault data. Brain = Obsidian. Muscle = Claude (Agent SDK, with MCP access to your vault + CalDAV). Voice = fully local STT/TTS, push-to-talk activated.

You already have the hard infrastructure done (Obsidian + Local REST API + MCP, CalDAV MCP via Odysseus). This plan builds the missing layers on top of that, in the order that de-risks the project fastest — including moving off the naive per-command approach early, before you've built more commands on top of it.

---

## Architecture

```
   [Push-to-talk keypress] → mlx-whisper (STT, local, Apple Silicon-native)
              ↓
     assistant process (Python, Claude Agent SDK — long-running,
     MCP connections stay warm, intent router in front)
              ↓
        streamed response text
              ↓
      Piper (TTS, local, streamed sentence-by-sentence) → speaker
              ↓
   local status API (FastAPI) ← psutil, vault summary, CalDAV, pipeline telemetry
              ↓
      HUD (Claude Design output) polls this API, renders on screen
```

Two loops running side by side: the **voice loop** (event-driven, triggered by a keypress) and the **HUD** (a dashboard that just polls a local API on an interval). They share the same backend data, but don't need to block each other.

---

## Component decisions

| Layer | Tool | Why |
|---|---|---|
| STT | `mlx-whisper` (large-v3-turbo) | Free, local, Metal-native — same MLX stack you already run Ollama models through on this MacBook |
| TTS | Piper | Free, local, low-latency; streamed sentence-by-sentence rather than waiting for the full reply |
| Orchestrator | Claude Agent SDK (Python) | In-process, not a subprocess per command — MCP connections stay warm, conversation state persists, supports streaming and tool-approval callbacks |
| Intent router | Simple regex/keyword matcher in front of the SDK | Trivial commands (time, vitals, "what's next") answered directly, no model call needed |
| Brain | Obsidian vault via existing Local REST API + MCP | Already built — reuse, don't rebuild |
| Schedule | CalDAV MCP (already in Odysseus) | Already built — reuse |
| Local API | FastAPI (Python) | Serves system vitals, vault summary, schedule, and pipeline telemetry to the HUD |
| HUD frontend | Claude Design | Prompted separately once the data API exists |

**Correction to the "Claude Code" framing from the post:** Claude Code's interactive terminal mode isn't what you want here either way. Two options: **headless mode** (`claude -p "prompt" --mcp-config vault-mcp.json`) shells out a fresh process per command — fine for a quick Phase 1 prototype, but each call re-initializes your MCP servers from scratch, which adds real latency once you're running this dozens of times a day. The **Claude Agent SDK** (Python) runs in-process instead — MCP connections stay warm, conversation state persists, you get streaming. Same subscription billing either way. Prototype with `claude -p`, migrate to the SDK in Phase 2 before building more on top of it.

---

## Billing

Headless/SDK usage currently draws from your normal Claude Pro subscription usage pool (same limits as interactive terminal use) — not separate API credits, unless you explicitly set `ANTHROPIC_API_KEY` for that process. Anthropic proposed splitting this into a separate metered credit pool starting June 15, 2026, but paused the change that same day; it's still paused as of now, with promised advance notice before any revised version ships. Since a chatty voice assistant shares your Pro plan's usage pool with everything else you do in Claude Code that day, keep an eye on whether you're bumping into session limits — the fallback is pointing this one process at your existing separate API key for pay-per-token billing instead.

Same pattern applies if you ever add Codex CLI (`codex exec`) to the mix — it authenticates via ChatGPT Plus and draws on that subscription rather than metered API billing, unless configured otherwise.

---

## Activation: push-to-talk vs wake-word

You're right to lean push-to-talk. Voice is an unauthenticated input channel — a keypress or hardware hotkey means the assistant only ever acts when you deliberately trigger it. That's the simpler, safer default, and it's what Phase 1 builds.

Wake-word ("Jarvis") is a legitimate upgrade later, not off the table. Porcupine runs a tiny local keyword-spotting model that only listens for the trigger phrase and doesn't record or transcribe anything until it fires — it's not sending audio anywhere. But it does mean the mic is continuously open and processing in the background, which is a different posture on a work machine than a keypress. Recommendation: build push-to-talk first, live with it, and add wake-word as a Phase 6 option only if push-to-talk starts to feel like friction. Keep a hotkey fallback even if you do add wake-word.

---

## Phase 0 — Audit & prep (~1 evening)

Since you already know the vault works, this is a refresh, not a rebuild:
- [ ] Confirm Obsidian Local REST API plugin is running and the token still works
- [ ] Confirm the Obsidian MCP server starts cleanly and Claude Code can list tools from it (`claude -p "list my MCP tools" --mcp-config vault-mcp.json --output-format json`)
- [ ] Skim vault folder structure — decide what "live data" the HUD should surface (e.g. today's daily note, open tasks, recent Cyber Security notes)
- [ ] Confirm CalDAV MCP still authenticates
- [ ] Confirm mlx-whisper installs cleanly on the MacBook (`pip install mlx-whisper`, needs `ffmpeg` via Homebrew)
- [ ] **Draft a one-page threat model** before writing any code: what inputs reach the agent (voice, vault content), what it can act on (filesystem writes, system commands), and which of those actions require explicit confirmation before executing. This shapes the tool-permission decisions in Phase 3, and it's a genuinely strong artifact for a security engineering portfolio — most people building this skip it entirely.

**Milestone:** one `claude -p` call round-trips through the Obsidian MCP server and returns real vault content.

---

## Phase 1 — Headless voice loop, proof of life (~2-3 evenings)

Build the core loop as a plain Python script, run from terminal. Naive shell-out is fine here — the point is proving the spine works before optimizing it:

1. Record audio on a push-to-talk keypress (deliberately not open listening — see Activation note above)
2. Transcribe with `mlx-whisper`
3. Shell out to `claude -p "<text>" --mcp-config vault-mcp.json --output-format json`, parse the JSON response
4. Feed the response text to Piper, play the audio
5. Build a `--text "..."` debug flag now, not later — it skips the mic entirely and runs typed input through the identical pipeline. You'll iterate far faster debugging this way, and it makes the rest of the project scriptable/testable.

**Milestone:** you can hold the key, say "what's on my schedule today," and hear a spoken answer pulled from CalDAV. This is proof the whole spine works — everything after this is refinement.

**Common mistake:** trying to keep a Claude Code session "alive" and feed it a stream of input via the CLI. Don't — that's what Phase 2's SDK migration is for; the CLI itself isn't built for that.

---

## Phase 2 — Move off the shell-out (~1-2 evenings)

This is the phase that separates a demo from something you'd actually use daily. Do it now, before Phase 3 adds more commands on top of the slow version:

- Rewrite the orchestrator using the **Claude Agent SDK** in-process instead of shelling out to `claude -p` per command. MCP connections stay warm across calls, conversation state persists, no per-call cold start.
- **Stream the response and feed Piper sentence-by-sentence** as text arrives, instead of waiting for the full reply. This is the single change that makes it feel responsive rather than laggy.
- Add the **intent router**: match trivial commands (time, system vitals, "what's next on my calendar") to direct function calls that skip the model entirely. Most of these projects call an LLM for everything and feel slow and expensive as a result — you don't need Claude to tell you the time.

**Milestone:** the same schedule-query command from Phase 1 now responds noticeably faster and starts speaking before the full response has finished generating.

---

## Phase 3 — Expand MCP command coverage, with guardrails (~2 evenings)

- Add vault-write commands ("add a task to my Linux Learning notes")
- Add system commands scoped to safe, whitelisted actions only
- Wire the Agent SDK's **tool approval callbacks** so destructive/write actions require explicit confirmation before executing — this is where the Phase 0 threat model gets enforced in code, not just documented. Treat any vault content retrieved into context as untrusted data, not instructions — a malicious string in a note you pasted months ago is a real prompt-injection surface once the agent has write access.
- Log every call — prompt, response, tools fired, per-stage timing (STT ms / model ms / TTS ms) — to a local file. Feeds both debugging and the Phase 5 HUD telemetry, and doubles as portfolio material, same as your Odysseus documentation.

**Milestone:** at least 3 distinct command types working reliably (query vault, query schedule, write a note with confirmation), and a destructive-sounding command actually pausing for your approval.

---

## Phase 4 — Local data API for the HUD (~1-2 evenings)

A small FastAPI service, separate from the voice loop, exposing:
- `GET /vitals` — CPU/GPU/RAM/disk via `psutil`
- `GET /schedule` — next few CalDAV events
- `GET /vault-summary` — e.g. today's daily note content, open task count (via Dataview query through the REST API)
- `GET /telemetry` — recent voice interactions, per-stage timing, which path handled each command (intent router vs. model), MCP tool calls fired

**Security note (this matters for your track, not just this project):** bind this to `127.0.0.1` only. Do not expose it on `0.0.0.0` on your LAN without authentication — it's a direct feed of your calendar, notes, and system state. If you ever want to hit it from another device, put it behind a reverse proxy with auth, don't just open the port.

**Milestone:** `curl localhost:PORT/vitals` and `/telemetry` both return real data.

---

## Phase 5 — Build the HUD (~1-2 evenings)

Now hand Claude Design the prompt from the post:

> "Build a dark terminal HUD for my OS. System vitals, command deck, schedule, audio I/O, live data from the vault, one screen, no tabs."

Point it at your Phase 4 API endpoints, including `/telemetry` — a command deck showing live pipeline timing and recent tool calls is genuinely useful; one showing decorative sci-fi bars with no real data behind them is a screensaver. Iterate on layout — single screen, no tabs is a real constraint, expect a few passes to get information density right.

**Milestone:** HUD on screen, auto-refreshing from your local API, showing real vitals/schedule/vault data and real pipeline telemetry.

---

## Phase 6 — Integration & polish (ongoing)

- Optional wake-word ("Jarvis") as an alternative to push-to-talk — see Activation note above for the tradeoff
- Visual feedback in the HUD when listening / processing / speaking
- Optional: route pure-coding commands to Codex CLI (`codex exec`, via ChatGPT Plus) while keeping vault/schedule commands on Claude, since the MCP servers are already wired there — only worth adding if you hit commands that genuinely don't need vault access
- Error handling: what the HUD shows if the MCP server, CalDAV, or STT/TTS pipeline drops

---

## Pitfalls to watch for

- **Latency stacking:** STT + LLM round-trip + TTS chains three slow steps. Measure each stage separately (this is what `/telemetry` is for) — don't guess which one is the bottleneck.
- **Silent MCP failures:** headless mode skips MCP servers that fail config validation instead of erroring out loudly. Always check `mcp_server_errors` in the JSON output, or a broken CalDAV connection will look like the assistant "just doesn't know" your schedule.
- **Vault write commands without guardrails:** giving voice-triggered write access to your vault is convenient but easy to get wrong (bad transcription → bad note edit). The Phase 3 tool-approval callbacks exist specifically for this.
- **Building the HUD before the loop works:** resist this — it's the most visually satisfying part and the least functional early on.
- **Building Phase 3+ on top of the Phase 1 shell-out:** do the Phase 2 SDK migration before you have a dozen commands to port over later.
- **It's your work machine:** an assistant with vault write access is a different risk profile than a coding tool you invoke on demand. Worth a quick check against your institute's device policy before it becomes a background process that starts on login.

---

## Next steps to actually start

1. Tonight: run the Phase 0 checklist, including the threat-model draft, and confirm the MCP round-trip works.
2. This week: get mlx-whisper + Piper installed and talking to each other with dummy text (skip Claude entirely at first — just prove STT→TTS works), and build the `--text` debug flag alongside it.
3. Then: wire in the headless `claude -p` call for Phase 1's milestone, and don't build past it before doing the Phase 2 SDK migration.
