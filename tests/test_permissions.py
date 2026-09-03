"""Tests for jarvis.orchestrator.permissions (Phase 3, Tasks 1 and 2).

Async coroutines are driven with asyncio.run() to match the existing async tests in
test_sdk_backend.py — the project does not use pytest-asyncio.

stdin redirection: every test monkeypatches sys.stdin to a small wrapper whose
.fileno() (and .read, for compatibility) delegate to the read end of an os.pipe().
This stands in for a real TTY fd and genuinely exercises loop.add_reader()/os.read();
the original sys.stdin is restored when the monkeypatch context exits.
"""

import asyncio
import os
import sys
import time
import types

import pytest

from jarvis.orchestrator import permissions


class _PipeStdin:
    def __init__(self, fd: int):
        self._fd = fd

    def fileno(self) -> int:
        return self._fd

    def read(self, n: int) -> str:
        return os.read(self._fd, n).decode(errors="replace")


@pytest.fixture
def pipe_stdin(monkeypatch):
    def _make() -> tuple[int, int, int]:
        r, w = os.pipe()
        monkeypatch.setattr(sys, "stdin", _PipeStdin(r))
        return r, w, r  # (read fd, write fd, fd to close on teardown)

    def teardown(*fds: int) -> None:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass

    return types.SimpleNamespace(make=_make, teardown=teardown)


def test_read_line_with_timeout_returns_none_on_timeout(pipe_stdin):
    r, w, _ = pipe_stdin.make()
    start = time.monotonic()
    result = asyncio.run(permissions.read_line_with_timeout("prompt: ", timeout=0.2))
    elapsed = time.monotonic() - start
    pipe_stdin.teardown(r, w)
    assert result is None
    assert 0.15 <= elapsed < 0.5  # hit the bound, but did not hang


def test_read_line_with_timeout_returns_real_input_promptly(pipe_stdin):
    r, w, _ = pipe_stdin.make()

    async def run():
        async def feed():
            await asyncio.sleep(0.1)
            os.write(w, b"yes\n")

        task = asyncio.create_task(feed())
        result = await permissions.read_line_with_timeout("prompt: ", timeout=5.0)
        await task
        return result

    start = time.monotonic()
    result = asyncio.run(run())
    elapsed = time.monotonic() - start
    pipe_stdin.teardown(r, w)
    assert result == "yes"
    assert elapsed < 1.0


def test_read_line_with_timeout_removes_reader_after_timeout(pipe_stdin):
    r, w, _ = pipe_stdin.make()

    async def run():
        first = await permissions.read_line_with_timeout("first: ", timeout=0.2)

        # If the first call's reader was still registered, this second call would
        # trip "Event loop is closed" / Bad file descriptor style breakage or
        # consume the input itself. It must behave cleanly.
        async def feed():
            await asyncio.sleep(0.1)
            os.write(w, b"second\n")

        task = asyncio.create_task(feed())
        second = await permissions.read_line_with_timeout("second: ", timeout=5.0)
        await task
        return first, second

    first, second = asyncio.run(run())
    pipe_stdin.teardown(r, w)
    assert first is None
    assert second == "second"


def test_confirmation_lock_serializes_two_concurrent_reads():
    events: list[str] = []

    async def run():
        async with permissions._confirmation_lock:
            events.append("first acquired")

            second_ran = asyncio.Event()

            async def second():
                async with permissions._confirmation_lock:
                    second_ran.set()
                    events.append("second acquired")

            task = asyncio.create_task(second())
            await asyncio.sleep(0.1)
            assert not second_ran.is_set()  # blocked while first holds the lock
            events.append("first released")

        await task
        assert second_ran.is_set()  # proceeded only after release

    asyncio.run(run())
    assert events == ["first acquired", "first released", "second acquired"]


# ---------------------------------------------------------------------------
# Task 2: tool matching, formatting, bypass switch, and the PreToolUse hook
# ---------------------------------------------------------------------------


def _hook(input_data):
    return asyncio.run(permissions.pre_tool_use_hook(input_data, "tool_1", None))


# --- _sanitize_preview ------------------------------------------------------


def test_sanitize_preview_strips_control_chars():
    result = permissions._sanitize_preview("a\x00b\x08c\x7fd")
    assert result == "abcd"


def test_sanitize_preview_neutralizes_ansi_escape():
    # The ESC byte (\x1b) opening an ANSI CSI sequence is in _CONTROL_CHARS' range,
    # so it's removed — the leftover plain-text parameters (e.g. "[31m") become inert,
    # unable to act as an escape sequence any longer.
    result = permissions._sanitize_preview("color\x1b[31mtext")
    assert "\x1b" not in result
    assert result.startswith("color")
    assert "text" in result


def test_sanitize_preview_makes_newlines_visible():
    assert permissions._sanitize_preview("line1\nline2") == "line1\\nline2"


def test_sanitize_preview_truncates_over_limit_with_ellipsis():
    long_text = "x" * (permissions.PREVIEW_MAX_LENGTH + 1)
    result = permissions._sanitize_preview(long_text)
    assert result == "x" * permissions.PREVIEW_MAX_LENGTH + "..."
    assert len(result) == permissions.PREVIEW_MAX_LENGTH + 3


def test_sanitize_preview_unchanged_at_or_under_limit():
    text = "y" * permissions.PREVIEW_MAX_LENGTH  # exactly at the limit
    assert permissions._sanitize_preview(text) == text
    assert permissions._sanitize_preview("short text") == "short text"


def test_sanitize_preview_empty_string():
    assert permissions._sanitize_preview("") == ""


# --- _format_pending_action: one case per tool in CONFIRMATION_REQUIRED_TOOLS ---


def test_format_pending_action_vault_append():
    result = permissions._format_pending_action(
        "mcp__obsidian__vault_append",
        {"path": "notes/today.md", "content": "new line"},
    )
    assert "vault_append" in result
    assert "notes/today.md" in result
    assert "new line" in result
    assert "destination" not in result
    assert "commandId" not in result


def test_format_pending_action_vault_copy():
    result = permissions._format_pending_action(
        "mcp__obsidian__vault_copy",
        {"path": "a.md", "destination": "b.md"},
    )
    assert "vault_copy" in result
    assert "a.md" in result
    assert "b.md" in result
    assert "->" in result
    assert "content" not in result


def test_format_pending_action_vault_delete():
    result = permissions._format_pending_action(
        "mcp__obsidian__vault_delete",
        {"path": "old/note.md"},
    )
    assert "vault_delete" in result
    assert "old/note.md" in result
    assert "destination" not in result
    assert "->" not in result
    assert "content" not in result


def test_format_pending_action_vault_move():
    result = permissions._format_pending_action(
        "mcp__obsidian__vault_move",
        {"path": "from.md", "destination": "to.md"},
    )
    assert "vault_move" in result
    assert "from.md" in result
    assert "to.md" in result
    assert "->" in result
    assert "content" not in result


def test_format_pending_action_vault_patch():
    result = permissions._format_pending_action(
        "mcp__obsidian__vault_patch",
        {"path": "doc.md", "content": "replacement text"},
    )
    assert "vault_patch" in result
    assert "doc.md" in result
    assert "replacement text" in result
    assert "destination" not in result
    assert "commandId" not in result


def test_format_pending_action_vault_write():
    result = permissions._format_pending_action(
        "mcp__obsidian__vault_write",
        {"path": "fresh.md", "content": "whole file"},
    )
    assert "vault_write" in result
    assert "fresh.md" in result
    assert "whole file" in result
    assert "destination" not in result


def test_format_pending_action_command_execute():
    result = permissions._format_pending_action(
        "mcp__obsidian__command_execute",
        {"commandId": "export-vault"},
    )
    assert "command_execute" in result
    assert "export-vault" in result
    assert "path" not in result
    assert "content" not in result


def test_format_pending_action_sanitizes_untrusted_fields():
    result = permissions._format_pending_action(
        "mcp__obsidian__vault_write",
        {"path": "ok.md", "content": "evil\x1b[31mtext\ninjected"},
    )
    assert "\x1b" not in result
    assert result.startswith("vault_write: ok.md")
    assert "evil[31mtext" in result          # leftover ANSI params, now inert
    assert "evil[31mtext\\ninjected" in result  # newline escaped visibly


# --- pre_tool_use_hook -----------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    ["mcp__obsidian__vault_read", "mcp__obsidian__vault_search", "some_other_tool"],
)
def test_hook_non_target_tool_returns_empty_without_reading_stdin(tool_name, monkeypatch):
    called = []
    monkeypatch.setattr(
        permissions,
        "read_line_with_timeout",
        lambda prompt, timeout: called.append(prompt),
    )
    monkeypatch.setenv("JARVIS_SKIP_CONFIRMATION", "")
    result = _hook({"tool_name": tool_name, "tool_input": {"path": "x.md"}})
    assert result == {}
    assert called == []


def _patch_no_claim(monkeypatch):
    """Isolate the terminal-only path: the HUD channel is unavailable because
    another confirmation is already pending (claim returns None)."""
    claims = []

    def fake_claim(tool_name, description, **kwargs):
        claims.append((tool_name, description, kwargs))
        return None

    monkeypatch.setattr(permissions.approvals, "claim", fake_claim)
    cleared = []
    monkeypatch.setattr(
        permissions.approvals, "clear", lambda pending_id, **kwargs: cleared.append(pending_id)
    )
    return claims, cleared


@pytest.mark.parametrize("answer", ["y", "Yes", "YES", " yes ", "yEs"])
@pytest.mark.parametrize("tool_name", sorted(permissions.CONFIRMATION_REQUIRED_TOOLS))
def test_hook_target_tool_allows_on_yes_answer(tool_name, answer, monkeypatch, capsys):
    monkeypatch.delenv("JARVIS_SKIP_CONFIRMATION", raising=False)
    _patch_no_claim(monkeypatch)

    async def fake_read(prompt, timeout):
        return answer

    monkeypatch.setattr(permissions, "read_line_with_timeout", fake_read)

    tool_input = {"path": "n.md", "content": "c", "commandId": "cmd"}
    result = _hook({"tool_name": tool_name, "tool_input": tool_input})
    out = capsys.readouterr().out
    assert "confirmation required" in out

    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert (
        result["hookSpecificOutput"]["permissionDecisionReason"]
        == "Confirmed by human via terminal."
    )


@pytest.mark.parametrize("answer", ["n", "no", "NO", "", "maybe", "gibberish"])
@pytest.mark.parametrize("tool_name", sorted(permissions.CONFIRMATION_REQUIRED_TOOLS))
def test_hook_target_tool_denies_on_non_yes_answer(tool_name, answer, monkeypatch, capsys):
    monkeypatch.delenv("JARVIS_SKIP_CONFIRMATION", raising=False)
    _patch_no_claim(monkeypatch)

    async def fake_read(prompt, timeout):
        return answer

    monkeypatch.setattr(permissions, "read_line_with_timeout", fake_read)

    result = _hook({"tool_name": tool_name, "tool_input": {"path": "n.md"}})
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        result["hookSpecificOutput"]["permissionDecisionReason"]
        == "Declined by human via terminal."
    )


@pytest.mark.parametrize("tool_name", sorted(permissions.CONFIRMATION_REQUIRED_TOOLS))
def test_hook_target_tool_denies_with_timeout_reason_on_none_answer(
    tool_name, monkeypatch
):
    monkeypatch.delenv("JARVIS_SKIP_CONFIRMATION", raising=False)
    _patch_no_claim(monkeypatch)
    monkeypatch.setattr(permissions, "CONFIRMATION_TIMEOUT_SECONDS", 0.3)

    async def fake_read(prompt, timeout):
        await asyncio.sleep(5.0)  # never answers in time
        return None

    monkeypatch.setattr(permissions, "read_line_with_timeout", fake_read)

    result = _hook({"tool_name": tool_name, "tool_input": {"path": "n.md"}})
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    lowered = reason.lower()
    # Must rule out the wrong diagnosis: Obsidian, its API, or connectivity.
    assert (
        "this is not an error with obsidian, its api, or network connectivity"
        in lowered
    ), f"Reason must rule out Obsidian/API/connectivity: {reason!r}"
    # Must attribute the cause to a lack of human response, not a plugin problem.
    assert "plugin" not in lowered, f"Reason must not point at plugins: {reason!r}"
    assert (
        "nobody answered" in lowered or "no response" in lowered
    ), f"Reason must explain it was a timeout with no answer: {reason!r}"
    # Only the terminal was offered on this path.
    assert "the terminal" in lowered, f"Reason must say the terminal was offered: {reason!r}"
    assert "the hud" not in lowered, f"Reason must not claim the HUD was offered: {reason!r}"


def test_hook_denies_when_no_terminal_and_no_claim(monkeypatch):
    """No terminal attached AND the single HUD slot is already taken by another
    pending confirmation — the old bare NoAttendedTerminalError case, now with a
    reason that names both failures."""
    monkeypatch.delenv("JARVIS_SKIP_CONFIRMATION", raising=False)
    _patch_no_claim(monkeypatch)

    async def fake_read(prompt, timeout):
        raise permissions.NoAttendedTerminalError(
            "stdin is not available for confirmation in this process"
        )

    monkeypatch.setattr(permissions, "read_line_with_timeout", fake_read)

    result = _hook({"tool_name": "mcp__obsidian__vault_write", "tool_input": {"path": "n.md"}})
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    lowered = reason.lower()
    assert "no interactive terminal" in lowered, f"Must name the missing terminal: {reason!r}"
    assert (
        "hud approval could not be offered" in lowered
    ), f"Must explain why no HUD channel existed: {reason!r}"
    assert (
        "this is not an error with obsidian, its api, or network connectivity" in lowered
    ), f"Reason must rule out Obsidian/API/connectivity: {reason!r}"


def test_hook_hud_wins_race_when_no_terminal(monkeypatch):
    """Terminal unavailable; a HUD claim succeeds and is decided shortly after —
    the HUD answer must win, and the reason must name the HUD channel."""
    monkeypatch.delenv("JARVIS_SKIP_CONFIRMATION", raising=False)
    monkeypatch.setattr(permissions, "CONFIRMATION_TIMEOUT_SECONDS", 5.0)

    state = {"decision": None}
    cleared = []

    def fake_claim(tool_name, description, **kwargs):
        return types.SimpleNamespace(id="rid-1")

    def fake_peek(**kwargs):
        return types.SimpleNamespace(id="rid-1", decision=state["decision"])

    def fake_decide(pending_id, decision, **kwargs):
        if pending_id == "rid-1" and state["decision"] is None:
            state["decision"] = decision
            return True
        return False

    monkeypatch.setattr(permissions.approvals, "claim", fake_claim)
    monkeypatch.setattr(permissions.approvals, "peek", fake_peek)
    monkeypatch.setattr(permissions.approvals, "decide", fake_decide)
    monkeypatch.setattr(
        permissions.approvals, "clear", lambda pending_id, **kwargs: cleared.append(pending_id)
    )

    async def fake_read(prompt, timeout):
        raise permissions.NoAttendedTerminalError(
            "stdin is not available for confirmation in this process"
        )

    monkeypatch.setattr(permissions, "read_line_with_timeout", fake_read)

    async def run():
        async def hud_answers():
            await asyncio.sleep(0.3)
            fake_decide("rid-1", "allow", decided_by="hud")

        task = asyncio.create_task(hud_answers())
        result = await permissions.pre_tool_use_hook(
            {"tool_name": "mcp__obsidian__vault_write", "tool_input": {"path": "n.md"}},
            "tool_1",
            None,
        )
        await task
        return result

    result = asyncio.run(run())
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert (
        result["hookSpecificOutput"]["permissionDecisionReason"]
        == "Confirmed by human via hud."
    )
    assert cleared == ["rid-1"]  # claim cleared exactly once after resolution


def test_hook_clear_called_once_on_deny_when_claim_succeeded(monkeypatch):
    monkeypatch.delenv("JARVIS_SKIP_CONFIRMATION", raising=False)
    monkeypatch.setattr(permissions, "CONFIRMATION_TIMEOUT_SECONDS", 5.0)

    state = {"decision": None}
    cleared = []

    def fake_claim(tool_name, description, **kwargs):
        return types.SimpleNamespace(id="rid-2")

    def fake_peek(**kwargs):
        return types.SimpleNamespace(id="rid-2", decision=state["decision"])

    def fake_decide(pending_id, decision, **kwargs):
        if pending_id == "rid-2" and state["decision"] is None:
            state["decision"] = decision
            return True
        return False

    monkeypatch.setattr(permissions.approvals, "claim", fake_claim)
    monkeypatch.setattr(permissions.approvals, "peek", fake_peek)
    monkeypatch.setattr(permissions.approvals, "decide", fake_decide)
    monkeypatch.setattr(
        permissions.approvals, "clear", lambda pending_id, **kwargs: cleared.append(pending_id)
    )

    async def fake_read(prompt, timeout):
        raise permissions.NoAttendedTerminalError(
            "stdin is not available for confirmation in this process"
        )

    monkeypatch.setattr(permissions, "read_line_with_timeout", fake_read)

    async def run():
        async def hud_denies():
            await asyncio.sleep(0.3)
            fake_decide("rid-2", "deny", decided_by="hud")

        task = asyncio.create_task(hud_denies())
        result = await permissions.pre_tool_use_hook(
            {"tool_name": "mcp__obsidian__vault_write", "tool_input": {"path": "n.md"}},
            "tool_1",
            None,
        )
        await task
        return result

    result = asyncio.run(run())
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        result["hookSpecificOutput"]["permissionDecisionReason"]
        == "Declined by human via hud."
    )
    assert cleared == ["rid-2"]


def test_hook_claim_called_with_formatted_description(monkeypatch):
    monkeypatch.delenv("JARVIS_SKIP_CONFIRMATION", raising=False)
    claims, _ = _patch_no_claim(monkeypatch)

    async def fake_read(prompt, timeout):
        return "y"

    monkeypatch.setattr(permissions, "read_line_with_timeout", fake_read)

    tool_name = "mcp__obsidian__vault_move"
    tool_input = {"path": "from.md", "destination": "to.md"}
    _hook({"tool_name": tool_name, "tool_input": tool_input})

    assert len(claims) == 1
    claimed_tool, claimed_desc, kwargs = claims[0]
    assert claimed_tool == tool_name
    assert claimed_desc == permissions._format_pending_action(tool_name, tool_input)
    assert kwargs.get("timeout_seconds") == 30.0


def test_hook_timeout_when_both_channels_offered_and_neither_answers(monkeypatch):
    """Terminal attached and a HUD claim succeeded, but nobody answered either —
    the reason must list both channels that were offered."""
    monkeypatch.delenv("JARVIS_SKIP_CONFIRMATION", raising=False)
    monkeypatch.setattr(permissions, "CONFIRMATION_TIMEOUT_SECONDS", 0.4)

    cleared = []
    monkeypatch.setattr(
        permissions.approvals,
        "claim",
        lambda tool_name, description, **kwargs: types.SimpleNamespace(id="rid-3"),
    )
    monkeypatch.setattr(
        permissions.approvals,
        "peek",
        lambda **kwargs: types.SimpleNamespace(id="rid-3", decision=None),
    )
    monkeypatch.setattr(
        permissions.approvals, "clear", lambda pending_id, **kwargs: cleared.append(pending_id)
    )

    async def fake_read(prompt, timeout):
        await asyncio.sleep(5.0)  # terminal never answers

    monkeypatch.setattr(permissions, "read_line_with_timeout", fake_read)

    result = _hook({"tool_name": "mcp__obsidian__vault_write", "tool_input": {"path": "n.md"}})
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    lowered = reason.lower()
    assert "the terminal" in lowered, f"Must mention the terminal was offered: {reason!r}"
    assert "the hud" in lowered, f"Must mention the HUD was offered: {reason!r}"
    assert (
        "no response was received within 0 seconds on" in lowered
    ), f"Must carry the timeout wording: {reason!r}"
    assert cleared == ["rid-3"]


def test_hook_hud_wins_and_terminal_task_is_cancelled(monkeypatch):
    """When the HUD answers first, the losing terminal task must be cancelled —
    not allowed to complete with a competing decision afterward."""
    monkeypatch.delenv("JARVIS_SKIP_CONFIRMATION", raising=False)
    monkeypatch.setattr(permissions, "CONFIRMATION_TIMEOUT_SECONDS", 5.0)

    state = {"decision": None}
    monkeypatch.setattr(
        permissions.approvals,
        "claim",
        lambda tool_name, description, **kwargs: types.SimpleNamespace(id="rid-4"),
    )
    monkeypatch.setattr(
        permissions.approvals,
        "peek",
        lambda **kwargs: types.SimpleNamespace(id="rid-4", decision=state["decision"]),
    )
    monkeypatch.setattr(
        permissions.approvals,
        "decide",
        lambda pending_id, decision, **kwargs: state.__setitem__("decision", decision)
        if pending_id == "rid-4" and state["decision"] is None
        else False,
    )
    monkeypatch.setattr(
        permissions.approvals, "clear", lambda pending_id, **kwargs: None
    )

    captured = {}

    async def fake_read(prompt, timeout):
        captured["task"] = asyncio.current_task()
        await asyncio.sleep(5.0)
        return "y"  # would be a competing "allow" if ever consumed

    monkeypatch.setattr(permissions, "read_line_with_timeout", fake_read)

    async def run():
        async def hud_answers():
            await asyncio.sleep(0.2)
            state["decision"] = "allow"

        task = asyncio.create_task(hud_answers())
        result = await permissions.pre_tool_use_hook(
            {"tool_name": "mcp__obsidian__vault_write", "tool_input": {"path": "n.md"}},
            "tool_1",
            None,
        )
        await task
        return result

    result = asyncio.run(run())
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert (
        result["hookSpecificOutput"]["permissionDecisionReason"]
        == "Confirmed by human via hud."
    )
    # The losing terminal task was cancelled, not completed with its own answer.
    assert captured["task"].cancelled() is True


def test_read_line_with_timeout_raises_no_attended_terminal_on_oserror(monkeypatch):
    """When ``loop.add_reader`` (or the stdin fd) raises OSError,
    ``read_line_with_timeout`` must raise NoAttendedTerminalError — not
    silently return None."""

    def _raise(*args, **kwargs):
        raise OSError("no interactive terminal available in this process")

    monkeypatch.setattr(asyncio.AbstractEventLoop, "add_reader", _raise)

    async def run():
        return await permissions.read_line_with_timeout("prompt: ", timeout=5.0)

    with pytest.raises(permissions.NoAttendedTerminalError):
        asyncio.run(run())


@pytest.mark.parametrize("env_value", ["1", "true", "yes", "on", " TRUE ", "Yes"])
@pytest.mark.parametrize("tool_name", sorted(permissions.CONFIRMATION_REQUIRED_TOOLS))
def test_hook_bypass_switch_allows_without_reading_stdin(
    tool_name, env_value, monkeypatch, capsys
):
    monkeypatch.setenv("JARVIS_SKIP_CONFIRMATION", env_value)

    called = []
    claims = []

    async def fake_read(prompt, timeout):
        called.append(prompt)
        return "y"

    monkeypatch.setattr(permissions, "read_line_with_timeout", fake_read)
    monkeypatch.setattr(
        permissions.approvals,
        "claim",
        lambda tool_name, description, **kwargs: claims.append((tool_name, description, kwargs)),
    )

    result = _hook({"tool_name": tool_name, "tool_input": {"path": "n.md"}})
    out = capsys.readouterr().out

    assert called == []  # stdin mechanism genuinely never reached
    assert claims == []  # no HUD claim and no wait happens at all
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert (
        result["hookSpecificOutput"]["permissionDecisionReason"]
        == "JARVIS_SKIP_CONFIRMATION active"
    )
    assert f"auto-approved {tool_name}" in out


@pytest.mark.parametrize("env_value", ["0", "false", "no", "off", "", "2"])
@pytest.mark.parametrize("tool_name", ["mcp__obsidian__vault_write"])
def test_hook_bypass_inactive_values_still_prompt(tool_name, env_value, monkeypatch, capsys):
    monkeypatch.setenv("JARVIS_SKIP_CONFIRMATION", env_value)
    _patch_no_claim(monkeypatch)

    async def fake_read(prompt, timeout):
        return "y"

    monkeypatch.setattr(permissions, "read_line_with_timeout", fake_read)

    result = _hook({"tool_name": tool_name, "tool_input": {"path": "n.md"}})
    out = capsys.readouterr().out
    assert "auto-approved" not in out
    assert "confirmation required" in out
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert (
        result["hookSpecificOutput"]["permissionDecisionReason"]
        == "Confirmed by human via terminal."
    )

