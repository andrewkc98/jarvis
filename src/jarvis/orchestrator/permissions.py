"""Confirmation gate for vault-mutating Obsidian tool calls (Phase 3)."""

from __future__ import annotations

import asyncio
import os
import re
import sys

CONFIRMATION_TIMEOUT_SECONDS = 30.0


class NoAttendedTerminalError(Exception):
    """Raised when stdin can't be registered with the event loop — no interactive
    terminal is attached to this process to confirm a pending action."""


CONFIRMATION_REQUIRED_TOOLS = frozenset({
    "mcp__obsidian__vault_append",
    "mcp__obsidian__vault_copy",
    "mcp__obsidian__vault_delete",
    "mcp__obsidian__vault_move",
    "mcp__obsidian__vault_patch",
    "mcp__obsidian__vault_write",
    "mcp__obsidian__command_execute",
})

PREVIEW_MAX_LENGTH = 200

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_confirmation_lock = asyncio.Lock()


async def read_line_with_timeout(prompt: str, timeout: float) -> str | None:
    """Print `prompt`, then read one line from stdin, genuinely cancellable and
    bounded by `timeout`. Returns the line (without trailing newline) on success,
    or None on timeout, EOF, or an unregisterable stdin file descriptor.

    Uses loop.add_reader() rather than a thread-blocked input() call — cancelling a
    thread blocked in input() does not stop it, which would leave the process unable
    to exit promptly after a timeout. This does.
    """
    print(prompt, end="", flush=True)
    loop = asyncio.get_running_loop()
    future: asyncio.Future[str | None] = loop.create_future()

    def _on_readable() -> None:
        try:
            data = os.read(sys.stdin.fileno(), 4096)
        except OSError:
            data = b""
        if not future.done():
            future.set_result(data.decode(errors="replace").rstrip("\n") if data else None)

    try:
        loop.add_reader(sys.stdin.fileno(), _on_readable)
    except OSError as exc:
        raise NoAttendedTerminalError(
            "stdin is not available for confirmation in this process"
        ) from exc

    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        loop.remove_reader(sys.stdin.fileno())


def _sanitize_preview(text: str) -> str:
    """Make untrusted tool-input text safe to print to a terminal: strip control/ANSI
    characters (which could otherwise manipulate the terminal display of this very
    prompt), escape newlines visibly, and bound the length."""
    text = _CONTROL_CHARS.sub("", text).replace("\n", "\\n")
    if len(text) > PREVIEW_MAX_LENGTH:
        return text[:PREVIEW_MAX_LENGTH] + "..."
    return text


def _format_pending_action(tool_name: str, tool_input: dict) -> str:
    """One human-readable line describing the pending action, using only the fields
    relevant to that specific tool. Every field pulled from tool_input goes through
    _sanitize_preview — never print raw untrusted values."""
    short_name = tool_name.removeprefix("mcp__obsidian__")
    if short_name in ("vault_delete", "vault_move"):
        path = _sanitize_preview(str(tool_input.get("path", "<unknown>")))
        if short_name == "vault_move":
            dest = _sanitize_preview(str(tool_input.get("destination", "<unknown>")))
            return f"{short_name}: {path} -> {dest}"
        return f"{short_name}: {path}"
    if short_name == "vault_copy":
        src = _sanitize_preview(str(tool_input.get("path", "<unknown>")))
        dest = _sanitize_preview(str(tool_input.get("destination", "<unknown>")))
        return f"vault_copy: {src} -> {dest}"
    if short_name in ("vault_write", "vault_append", "vault_patch"):
        path = _sanitize_preview(str(tool_input.get("path", "<unknown>")))
        content = _sanitize_preview(str(tool_input.get("content", "")))
        return f"{short_name}: {path}\n  content preview: {content}"
    if short_name == "command_execute":
        command_id = _sanitize_preview(str(tool_input.get("commandId", tool_input)))
        return f"command_execute: {command_id}"
    # Should be unreachable given CONFIRMATION_REQUIRED_TOOLS, but never crash on an
    # unexpected shape — fall back to a generic, still-sanitized description.
    return f"{short_name}: {_sanitize_preview(str(tool_input))}"


def _skip_confirmation_active() -> bool:
    """Checked fresh on every call, not cached, so it takes effect for a run without
    a code change — set JARVIS_SKIP_CONFIRMATION before starting Jarvis."""
    return os.environ.get("JARVIS_SKIP_CONFIRMATION", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


async def pre_tool_use_hook(input_data, tool_use_id, context):
    """PreToolUse hook: gates exactly the 7 tools in CONFIRMATION_REQUIRED_TOOLS.
    Every other tool call returns {} immediately — an empty hook response makes no
    permission decision, so evaluation falls through to the unchanged
    bypassPermissions mode with no added latency and no prompt."""
    tool_name = input_data.get("tool_name", "")
    if tool_name not in CONFIRMATION_REQUIRED_TOOLS:
        return {}

    if _skip_confirmation_active():
        print(
            f"[jarvis] auto-approved {tool_name} (JARVIS_SKIP_CONFIRMATION active)",
            flush=True,
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "JARVIS_SKIP_CONFIRMATION active",
            }
        }

    tool_input = input_data.get("tool_input", {})
    description = _format_pending_action(tool_name, tool_input)

    try:
        async with _confirmation_lock:
            print(f"\n[jarvis] confirmation required:\n  {description}")
            answer = await read_line_with_timeout(
                "Confirm? [y/N]: ", timeout=CONFIRMATION_TIMEOUT_SECONDS
            )
    except NoAttendedTerminalError:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "No interactive terminal is available to confirm this action. "
                    "This process needs to be running attended, in a real terminal "
                    "session, for vault-mutating requests. This is not an error with "
                    "Obsidian, its API, or network connectivity."
                ),
            }
        }

    if answer is not None and answer.strip().lower() in ("y", "yes"):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "Confirmed by human",
            }
        }

    if answer is None:
        reason = (
            f"No response was received within {CONFIRMATION_TIMEOUT_SECONDS:.0f} "
            "seconds. This write was denied by default for safety and was never "
            "attempted — this is not an error with Obsidian, its API, or network "
            "connectivity; it means nobody answered the confirmation prompt in time. "
            "If the human wants this to happen, ask again and explicitly respond to "
            "the terminal confirmation."
        )
    else:
        reason = "Declined by human."
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
