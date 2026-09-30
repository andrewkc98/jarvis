"""Credential loading and generated MCP configuration helpers."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import keyring


MCP_CONFIG_PATH = Path(__file__).resolve().parents[2] / "mcp-config.json"

API_PORT_ENV_VAR = "JARVIS_API_PORT"
_DEFAULT_API_PORT = 8765
VOICE_INPUT_MODE_ENV_VAR = "JARVIS_VOICE_INPUT_MODE"
_VALID_VOICE_INPUT_MODES = {"terminal", "browser", "off"}
_DEFAULT_VOICE_INPUT_MODE = "off"
_INVALID_VOICE_INPUT_MODE_MESSAGE = f"Invalid {VOICE_INPUT_MODE_ENV_VAR}"


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


def _load_voice_input_mode() -> str:
    raw = os.environ.get(VOICE_INPUT_MODE_ENV_VAR)
    if raw is None:
        return _DEFAULT_VOICE_INPUT_MODE
    if raw in _VALID_VOICE_INPUT_MODES:
        return raw
    raise RuntimeError(_INVALID_VOICE_INPUT_MODE_MESSAGE)


API_PORT = _load_api_port()
VOICE_INPUT_MODE = _load_voice_input_mode()
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
    """Render a template's ``${VAR_NAME}`` placeholders and write it securely.

    The template is rendered completely before anything is published. The
    rendered text is then written to a uniquely named temporary file with mode
    ``0600`` at creation, flushed and ``fsync``-ed, and finally moved into
    place with an atomic ``os.replace``. On any failure before the
    replacement, only that attempt's temporary file is removed and any existing
    destination is left untouched; a successful replacement leaves the
    destination at mode ``0600``.
    """

    rendered = _PLACEHOLDER_PATTERN.sub(
        lambda match: get_credential(match.group(1)), template_path.read_text()
    )

    output_path = output_path.resolve()
    tmp_path = output_path.parent / (
        f".{output_path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    )
    succeeded = False
    try:
        # Create with 0600 at creation time so the credential-bearing content
        # is never world-readable, regardless of the process umask.
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                tmp_file.write(rendered)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        os.replace(tmp_path, output_path)
        succeeded = True
    finally:
        if not succeeded:
            tmp_path.unlink(missing_ok=True)
    # os.replace preserves the source mode (already 0600), but normalise the
    # destination defensively in case the target already existed.
    os.chmod(output_path, 0o600)
