"""Routes a transcript to a direct provider or the SDK-backed fallback, with no
model/MCP round-trip for provider-backed commands."""

from __future__ import annotations

import re


_SCHEDULE_PATTERN = re.compile(r"\b(schedule|calendar)\b", re.IGNORECASE)


def route(text: str) -> str:
    """Return "schedule" if *text* is a whole-word schedule/calendar request, else
    "fallback"."""
    if _SCHEDULE_PATTERN.search(text):
        return "schedule"
    return "fallback"
