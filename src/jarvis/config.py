"""Credential loading and generated MCP configuration helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path

import keyring


MCP_CONFIG_PATH = Path(__file__).resolve().parents[2] / "mcp-config.json"


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
