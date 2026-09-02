# MCP & External Service Inventory

Status: written 2026-09-01 by Claude Code, from live inspection of this machine (LaunchAgent
plist, running process, direct curl/tool probing) — not from documentation or assumption.
This is a factual record for `.agent/PLAN.md` Phase 1b implementation to read, not a
design document. Update it if any of the underlying services change.

## Obsidian — Local REST API + MCP

### Legacy SSE bridge (deprecated 2026-09-02) — kept for reference only

**Historical architecture:** `supergateway` wrapped the stdio `mcp-obsidian` server and exposed it as
an SSE endpoint. It ran as a macOS LaunchAgent
(`~/Library/LaunchAgents/com.odysseus.obsidian-mcp.plist`, label
`com.odysseus.obsidian-mcp`), with `KeepAlive=true`; `launchctl list` confirmed a live
PID and it auto-restarted on crash/login.

```
claude CLI --mcp-config ──▶ http://localhost:3100/sse (supergateway, SSE)
                                    │
                                    ▼
                          uvx mcp-obsidian (stdio child process)
                                    │  env: OBSIDIAN_HOST=https://localhost:27124
                                    │       OBSIDIAN_API_KEY=<redacted>
                                    │       OBSIDIAN_INSECURE=true
                                    ▼
                     Obsidian Local REST API plugin (self-signed cert)
```

**Historical MCP config entry** (verified at the time: `GET /sse` returned `200`;
`GET /mcp` returns `404`, so this instance does not speak the newer streamable-HTTP
transport, only SSE; `GET /` returned a generic 404 — no auth challenge, meaning the SSE
endpoint itself was unauthenticated on localhost — the real Obsidian token was consumed
internally by the `mcp-obsidian` child process, not by the CLI's connection to
supergateway):
```json
{
  "mcpServers": {
    "obsidian": {
      "type": "sse",
      "url": "http://localhost:3100/sse"
    }
  }
}
```
No `Authorization` header was needed on this legacy entry because supergateway did not
check it.

This bridge is deprecated because repeated `claude` client connections crash it with
`Already connected to a transport`, confirmed directly. It was replaced for that
concrete failure, not merely superseded by a newer option.

**Real tool names** (confirmed by direct enumeration against a live `mcp-obsidian`
connection, not documentation). The bare name is the MCP tool name itself; **the ID
`claude -p --allowedTools` actually needs is the fully qualified `mcp__<server>__<tool>`
form** — confirmed during Phase 1b implementation/review, since `--allowedTools` with the
bare name silently failed to match. The configured server name is `obsidian` (see the
`mcp-config.json` entry above), so every ID below is `mcp__obsidian__<bare name>`:

| Tool (bare name) | Fully qualified `--allowedTools` ID | Read/Write |
|---|---|---|
| `obsidian_list_files_in_vault` | `mcp__obsidian__obsidian_list_files_in_vault` | Read |
| `obsidian_list_files_in_dir` | `mcp__obsidian__obsidian_list_files_in_dir` | Read |
| `obsidian_get_file_contents` | `mcp__obsidian__obsidian_get_file_contents` | Read |
| `obsidian_batch_get_file_contents` | `mcp__obsidian__obsidian_batch_get_file_contents` | Read |
| `obsidian_get_frontmatter` | `mcp__obsidian__obsidian_get_frontmatter` | Read |
| `obsidian_get_periodic_note` | `mcp__obsidian__obsidian_get_periodic_note` | Read |
| `obsidian_get_recent_periodic_notes` | `mcp__obsidian__obsidian_get_recent_periodic_notes` | Read |
| `obsidian_get_recent_changes` | `mcp__obsidian__obsidian_get_recent_changes` | Read |
| `obsidian_simple_search` | `mcp__obsidian__obsidian_simple_search` | Read |
| `obsidian_complex_search` | `mcp__obsidian__obsidian_complex_search` | Read |
| `obsidian_search_by_tag` | `mcp__obsidian__obsidian_search_by_tag` | Read |
| `obsidian_append_content` | `mcp__obsidian__obsidian_append_content` | **Write** |
| `obsidian_patch_content` | `mcp__obsidian__obsidian_patch_content` | **Write** |
| `obsidian_put_content` | `mcp__obsidian__obsidian_put_content` | **Write** (full overwrite) |
| `obsidian_delete_file` | `mcp__obsidian__obsidian_delete_file` | **Write/destructive** |

Under the retired restricted-tool design (superseded 2026-09-02 by the accepted
full-capability, no-isolation decision), Phase 1b/2's `allowed_tools` list would have
been exactly the 11 fully qualified Read IDs above, and the 4 fully qualified Write IDs
would have been what Phase 3's `confirmation_required_tools` list contained when write
support is added. **This restriction is not applied in the current implementation** —
`cli_backend.py` runs with `--permission-mode bypassPermissions` and no
`allowedTools`/`disallowedTools`/`--strict-mcp-config` restriction, so every tool listed
above (and any other ambient MCP tool) is available without an allow-list gate.

**Dataview query capability, for `/vault-summary` (Phase 4):** there is no literal
Dataview-DQL tool. `obsidian_complex_search` takes a **JsonLogic** query (custom operators
`glob` and `regexp` over `path`/`content`), not Dataview query language. An open-task count
is achievable via `obsidian_complex_search` with a `regexp` match for a task-checkbox
pattern (e.g. `- \[ \]`) across vault files. Treat "Dataview via REST API" as **not
available**; plan accordingly, this isn't a gap to re-investigate.

**Periodic notes and `providers/vault_provider.py` — resolved 2026-09-02.** Live testing
confirmed `OPTIONS /periodic/daily/` returns `204` (the route is registered) while
authenticated `GET /periodic/daily/` returns `404` with the Local REST API's own JSON
body `errorCode` `40461` — the plugin's documented code for "today's target daily-note/
file not found," i.e. no daily note has been created yet, not a broken or misconfigured
route. The human has not asked Jarvis to create a note. `vault_provider.py` now treats
only this exact combination (`status_code == 404` and JSON `errorCode` `40461`) as an
empty daily note (`get_daily_note()` returns `""`, so `get_open_task_count()` returns
`0`); every other HTTP error, a malformed/non-JSON body, or a 404 with any other
`errorCode` still raises, unchanged. This is independent of the native MCP endpoint's
`periodic_note_get_path` tool; MCP and the plain REST route remain different surfaces —
this fix only concerns the direct REST client.

### Native MCP endpoint (current, since 2026-09-02)

The Obsidian Local REST API plugin was updated to version `5.1.0`, which exposes its own
native MCP endpoint directly. It uses no `supergateway` and no `mcp-obsidian` stdio child
process. The human explicitly enabled this endpoint's loopback HTTP listener in the
Obsidian plugin's own settings.

- Endpoint: `http://127.0.0.1:27123/mcp/`
- Transport: streamable HTTP (MCP config `"type": "http"`)
- Network scope: loopback only
- Authentication: `Authorization: Bearer <OBSIDIAN_REST_TOKEN>` is required. Unlike the
  legacy SSE bridge, this endpoint is authenticated; this is a real transport change.

A direct authenticated protocol probe initialized successfully and reported 17 tools
total, including the confirmed tool `periodic_note_get_path`. A live Claude enumeration
returned the following exact list (17 tools, matching the prior protocol probe):

- `mcp__obsidian__active_file_get_path`
- `mcp__obsidian__command_execute`
- `mcp__obsidian__command_list`
- `mcp__obsidian__open_file`
- `mcp__obsidian__periodic_note_get_path`
- `mcp__obsidian__search_query`
- `mcp__obsidian__search_simple`
- `mcp__obsidian__tag_list`
- `mcp__obsidian__vault_append`
- `mcp__obsidian__vault_copy`
- `mcp__obsidian__vault_delete`
- `mcp__obsidian__vault_get_document_map`
- `mcp__obsidian__vault_list`
- `mcp__obsidian__vault_move`
- `mcp__obsidian__vault_patch`
- `mcp__obsidian__vault_read`
- `mcp__obsidian__vault_write`

**Current `mcp-config.json` entry** (matches `mcp-config.json.example` after Task 1):
```json
{
  "mcpServers": {
    "obsidian": {
      "type": "http",
      "url": "http://127.0.0.1:27123/mcp/",
      "headers": {
        "Authorization": "Bearer ${OBSIDIAN_REST_TOKEN}"
      }
    }
  }
}
```

**Current `cli_backend.py` invocation:**
```
claude -p "<prompt>" \
  --mcp-config <generated mcp-config.json path> \
  --permission-mode bypassPermissions \
  --output-format json
```
Run with `cwd` set to a dedicated neutral scratch directory, not the Jarvis repository.
The isolation flags `--strict-mcp-config`, `--tools ""`, `--allowedTools`,
`--permission-mode dontAsk`, and `--setting-sources ""` were tried and found to silently
break on the installed `claude` version (`--strict-mcp-config` defeats `--tools ""`).
They were deliberately dropped by an explicit human decision and are historical context,
not a live recommendation.

## Schedule — **not CalDAV, not an MCP server; see below**

The source planning doc's assumption ("CalDAV MCP already in Odysseus") does not match
what's actually installed. `~/odysseus` is a full personal-assistant platform (its own
FastAPI app, SQLite DB, calendar sync, and an MCP *client* manager for connecting to
*other* MCP servers) — it does not expose calendar as an MCP server itself, and the
service was not running at the time of this inventory.

**Decision (confirmed with the human, 2026-09-01):** `providers/schedule_provider.py`
reads from **macOS Calendar.app via EventKit** instead — no CalDAV credentials, no
dependency on Odysseus being installed or running. See `.agent/PLAN.md`'s Phase 1b
section and Component decisions table for the design; this file just records that the
decision was made and why raw CalDAV/Odysseus were both rejected.

**EventKit API surface, verified by installing `pyobjc-framework-EventKit` in a scratch
venv and introspecting the real installed package (not documentation) on this machine:**
- `EventKit.EKEventStore.alloc().init()` — construct the store.
- `store.requestAccessToEntityType_completion_(EventKit.EKEntityTypeEvent, completion)` —
  real, confirmed present. `completion` is a Python callable `(granted: bool, error) ->
  None`. **Unverified in this pass:** whether the completion callback fires without
  manually pumping an `NSRunLoop`/`CFRunLoop` in a plain script context (vs. a full Cocoa
  app) — this needs a small standalone empirical test before relying on it, and it
  triggers a real macOS system permission dialog the first time, which needs the human
  present to click "Allow." Do not attempt to script around or suppress that dialog.
- `EventKit.EKEntityTypeEvent` = `0`, `EventKit.EKEntityTypeReminder` = `1` — confirmed.
- `EventKit.EKAuthorizationStatusNotDetermined` = `0`, `Restricted` = `1`, `Denied` = `2`,
  `Authorized`/`FullAccess` = `3`, `WriteOnly` = `4` — confirmed.
- `store.calendarsForEntityType_(EventKit.EKEntityTypeEvent)` — confirmed present, returns
  the list of calendars to pass into the predicate below.
- `store.predicateForEventsWithStartDate_endDate_calendars_(start, end, calendars)` —
  confirmed present (an `NSPredicate`-like object, not a Python callable).
- `store.eventsMatchingPredicate_(predicate)` — confirmed present, synchronous, returns
  an array of `EKEvent` objects (no callback/run-loop concern for this call specifically,
  only for the access-request call above).
- `EKEvent` public properties confirmed present: `.title()`, `.startDate()`, `.endDate()`,
  `.isAllDay()`, `.notes()`, `.calendar()`.
- A newer-looking `requestAccessToEntityType_desiredFullAccess_testing_synchronous_reason_completion_`
  method also exists on this system but was not investigated — its name suggests it may be
  an internal/testing variant, not a stable public API to build on. Stick to the plain
  `requestAccessToEntityType_completion_` method above unless a specific need arises.

## Claude CLI tool-isolation flags — verified via `claude -p --help` on this machine

(`claude --version` = `2.1.247 (Claude Code)`)

This table documents flag semantics for reference only. None of the isolation flags
below (`--tools`, `--allowedTools`, `--disallowedTools`, `--strict-mcp-config`,
`--setting-sources`) are used by the current implementation — see "Current
`cli_backend.py` flag set" immediately after the table.

| Flag | Confirmed behavior |
|---|---|
| `--tools <tools...>` | "Specify the list of available tools from the built-in set. Use `\"\"` to disable all tools." This is the flag that would remove the entire built-in toolset (Bash/Read/Write/Edit/...) in one step if tool isolation were reinstated; it is **not used** in the current `cli_backend.py` invocation. |
| `--allowedTools` / `--allowed-tools <tools...>` | Comma/space-separated allow list. **Must be the fully qualified `mcp__<server>__<tool>` form for MCP tools** — a bare tool name (e.g. `obsidian_list_files_in_vault`) silently fails to match; confirmed during Phase 1b implementation, not just theorized. |
| `--disallowedTools` / `--disallowed-tools <tools...>` | Comma/space-separated deny list. |
| `--permission-mode <mode>` | Choices confirmed: `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan`. `dontAsk` is real and spelled exactly as the Python SDK spells it. |
| `--setting-sources <sources>` | "Comma-separated list of setting sources to load (user, project, local)." To load none, pass an empty string: `--setting-sources ""`. |
| `--strict-mcp-config` | "Only use MCP servers from `--mcp-config`, ignoring all other MCP configurations." Exact flag name confirmed. **Not used** by `cli_backend.py` — found to silently defeat `--tools ""` on the installed `claude` version during Phase 1b testing, and dropped along with the rest of the isolation flags by the human's explicit decision. |
| `--mcp-config <configs...>` | "Load MCP servers from JSON files or strings (space-separated)." |

**Current `cli_backend.py` flag set:**
```
claude -p "<prompt>" \
  --mcp-config <generated mcp-config.json path> \
  --permission-mode bypassPermissions \
  --output-format json
```
Run with `cwd` set to a dedicated neutral scratch directory, not the Jarvis repository.
The isolation flags `--strict-mcp-config`, `--tools ""`, `--allowedTools`,
`--permission-mode dontAsk`, and `--setting-sources ""` were tried and found to silently
break on the installed `claude` version (`--strict-mcp-config` defeats `--tools ""`).
They were deliberately dropped by an explicit human decision and are historical context,
not a live recommendation.
