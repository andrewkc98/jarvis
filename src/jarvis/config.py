"""Credential loading and generated MCP configuration helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path

import keyring


MCP_CONFIG_PATH = Path(__file__).resolve().parents[2] / "mcp-config.json"

API_PORT_ENV_VAR = "JARVIS_API_PORT"
_DEFAULT_API_PORT = 8765


def _load_api_port() -> int:
    raw = os.environ.get(API_PORT_ENV_VAR)
    if raw is None:
        return _DEFAULT_API_PORT
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{API_PORT_ENV_VAR}={raw!r} is not a valid integer port"
        ) from exc


API_PORT = _load_api_port()
HUD_ORIGIN = os.environ.get("HUD_ORIGIN") or None
TELEMETRY_PATH = Path(__file__).resolve().parents[2] / "telemetry.jsonl"
TELEMETRY_LOCK_PATH = TELEMETRY_PATH.with_suffix(".jsonl.lock")
PENDING_APPROVAL_PATH = Path(__file__).resolve().parents[2] / "pending_approval.json"
PENDING_APPROVAL_LOCK_PATH = PENDING_APPROVAL_PATH.with_suffix(".json.lock")
RUNTIME_STATUS_PATH = Path(__file__).resolve().parents[2] / "runtime_status.json"
RUNTIME_STATUS_LOCK_PATH = RUNTIME_STATUS_PATH.with_suffix(".json.lock")
VOICE_MODEL_PATH = os.environ.get("JARVIS_VOICE_MODEL") or None


def get_credential(name: str) -> str:
    """Return a credential from the environment or the macOS Keychain."""

    value = os.environ.get(name)
    if value is not None:
        return value
    value = keyring.get_password("jarvis", name)
    if value is not None:
        return value
    raise RuntimeError(f"Missing credential: {name}")


_PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def generate_mcp_config(template_path: Path, output_path: Path) -> None:
    """Render a template's ``${VAR_NAME}`` placeholders and write it securely."""

    rendered = _PLACEHOLDER_PATTERN.sub(
        lambda match: get_credential(match.group(1)), template_path.read_text()
    )
    output_path.write_text(rendered)
    os.chmod(output_path, 0o600)
