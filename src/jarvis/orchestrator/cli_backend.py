"""Claude CLI backend for open-ended vault queries with full tool/MCP capability
(accepted-risk bypassPermissions posture; see docs/threat-model.md)."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from jarvis import config


_MCP_TEMPLATE_PATH = config.MCP_CONFIG_PATH.with_name("mcp-config.json.example")
_SCRATCH_CWD = Path(tempfile.gettempdir()) / "jarvis-cli-scratch"
_TIMEOUT_SECONDS = 60
_MAX_DIAGNOSTIC_LENGTH = 2000


def _bounded_diagnostic(value: str) -> str:
    """Keep CLI diagnostics useful without exposing an unbounded response."""

    if len(value) <= _MAX_DIAGNOSTIC_LENGTH:
        return value
    return f"{value[:_MAX_DIAGNOSTIC_LENGTH]}..."


def ask(prompt: str) -> str:
    """Ask Claude a vault question. Runs with full built-in tool access and the
    configured MCP server(s), no tool restriction (--permission-mode bypassPermissions)."""

    config.generate_mcp_config(_MCP_TEMPLATE_PATH, config.MCP_CONFIG_PATH)
    cmd = [
        "claude",
        "-p",
        prompt,
        "--mcp-config",
        str(config.MCP_CONFIG_PATH),
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "json",
    ]

    try:
        _SCRATCH_CWD.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
            cwd=str(_SCRATCH_CWD),
        )
    except subprocess.TimeoutExpired as exc:
        detail = f": {exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr}" if exc.stderr else ""
        raise RuntimeError(
            f"Claude CLI timed out after {_TIMEOUT_SECONDS} seconds{detail}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Unable to run Claude CLI: {exc}") from exc

    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        stdout = (completed.stdout or "").strip()
        if stdout:
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                result = payload.get("result")
                if isinstance(result, str) and result.strip():
                    raise RuntimeError(
                        "Claude CLI exited with "
                        f"status {completed.returncode}: {_bounded_diagnostic(result)}"
                    )
        detail = f": {_bounded_diagnostic(stderr)}" if stderr else ""
        raise RuntimeError(f"Claude CLI exited with status {completed.returncode}{detail}")

    stdout = (completed.stdout or "").strip()
    if not stdout:
        detail = f"; stderr: {stderr}" if stderr else ""
        raise RuntimeError(f"Claude CLI returned empty stdout{detail}")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        detail = f"; stderr: {stderr}" if stderr else ""
        raise RuntimeError(f"Claude CLI returned malformed JSON{detail}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Claude CLI JSON response must be an object")

    mcp_errors = payload.get("mcp_server_errors")
    if mcp_errors:
        raise RuntimeError(f"Claude MCP server errors: {mcp_errors}")
    if payload.get("is_error"):
        result = payload.get("result")
        detail = f": {result}" if result else ""
        raise RuntimeError(f"Claude CLI reported an error{detail}")

    result = payload.get("result")
    if not isinstance(result, str):
        raise RuntimeError("Claude CLI JSON response is missing a string result field")
    return result
