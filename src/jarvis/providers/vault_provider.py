"""Read daily notes and task counts through Obsidian's Local REST API."""

from __future__ import annotations

import httpx

from jarvis.config import get_credential


_DEFAULT_BASE_URL = "https://localhost:27124"
_DAILY_NOTE_PATH = "/periodic/daily/"
_MISSING_DAILY_NOTE_ERROR_CODE = 40461


def _is_missing_daily_note(response: httpx.Response) -> bool:
    """True only for the Local REST API's documented "note not found" response."""
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("errorCode") == _MISSING_DAILY_NOTE_ERROR_CODE


def _base_url() -> tuple[str, bool]:
    """Return the configured API URL and whether its default is being used."""
    try:
        return get_credential("OBSIDIAN_REST_BASE_URL"), False
    except RuntimeError as error:
        # The credential helper uses this error for an unset environment/keychain
        # value; other credential failures should not be silently hidden.
        if str(error) != "Missing credential: OBSIDIAN_REST_BASE_URL":
            raise
        return _DEFAULT_BASE_URL, True


def _fetch_daily_note() -> str:
    base_url, using_default = _base_url()
    token = get_credential("OBSIDIAN_REST_TOKEN")
    # The documented local default uses Obsidian's self-signed HTTPS certificate.
    # Explicitly configured URLs retain normal TLS certificate verification.
    verify = False if using_default else True
    with httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        verify=verify,
    ) as client:
        response = client.get(_DAILY_NOTE_PATH)
        if response.status_code == 404 and _is_missing_daily_note(response):
            return ""
        response.raise_for_status()
        return response.text


def get_daily_note() -> str:
    """Fetch today's daily note as plain Markdown text."""
    return _fetch_daily_note()


def get_open_task_count() -> int:
    """Count unchecked Markdown task-list items in today's daily note."""
    return sum(line.startswith("- [ ]") for line in get_daily_note().splitlines())
