"""Durable telemetry store for the HUD local data API.

The store is a single JSONL file (``config.TELEMETRY_PATH``) guarded by a
cross-process advisory lock (``config.TELEMETRY_LOCK_PATH``) acquired with
``fcntl.flock``. Appends and rotation are performed *while holding the
exclusive lock*, so a concurrent reader either sees the file before the append
or after the (atomic) rotation — never a torn or partially-written line.

Only safe-by-construction fields are written: the pipeline's path name, a
measured duration, and which tools fired. Prompts, responses, and any
vault/calendar content are explicitly out of scope and are never persisted here.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis import config

SCHEMA_VERSION = 1

# Retention: keep entries newer than this AND among the most recent N.
RETENTION_SECONDS = 30 * 24 * 60 * 60  # 30 days
MAX_RETAINED_ENTRIES = 5000

# Rotation only rewrites the file once it has grown comfortably above the
# retention limit, so a single append is not an O(N) rewrite every time.
# 5,500 is *meaningfully* above MAX_RETAINED_ENTRIES (5,000) by design.
ROTATION_LINE_THRESHOLD = 5500

_VALID_PATHS = ("schedule", "sdk")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware ``datetime``; raise ``ValueError``."""

    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"unparseable timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class TelemetryEntry:
    """A single validated telemetry record.

    All fields are validated in ``__post_init__``; construction raises
    ``ValueError`` on an unrecognized ``path``, a negative duration, a
    non-list ``tools_fired``, or an unparseable ``timestamp``.
    """

    schema_version: int = SCHEMA_VERSION
    timestamp: str = field(default_factory=_now_iso)
    path: str = "sdk"
    duration_ms: float = 0.0
    stt_ms: float | None = None
    dispatch_ms: float = 0.0
    tools_fired: list[str] = field(default_factory=list)
    error: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version!r}")
        if self.path not in _VALID_PATHS:
            raise ValueError(f"path must be one of {_VALID_PATHS}, got {self.path!r}")
        self.duration_ms = round(float(self.duration_ms), 1)
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be >= 0")
        self.dispatch_ms = round(float(self.dispatch_ms), 1)
        if self.dispatch_ms < 0:
            raise ValueError("dispatch_ms must be >= 0")
        if self.stt_ms is not None:
            self.stt_ms = float(self.stt_ms)
        if not isinstance(self.tools_fired, list):
            raise ValueError("tools_fired must be a list of tool names")
        # Ensures the timestamp actually parses; raises ValueError otherwise.
        _parse_timestamp(self.timestamp)

    def to_json_line(self) -> str:
        """Serialize to a single JSON object plus a trailing newline."""

        payload = dataclasses.asdict(self)
        return json.dumps(payload, separators=(",", ":")) + "\n"


def parse_line(line: str) -> TelemetryEntry:
    """Parse one JSONL line back into a ``TelemetryEntry``.

    Raises ``ValueError`` on bad JSON, a non-object line, a missing field, a
    different ``schema_version``, or any field-validation failure. This is the
    single canonical validator shared by rotation (drop-on-rewrite) and
    ``read_recent`` (skip-on-read).
    """

    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"line is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("line is not a JSON object")

    required = (
        "schema_version",
        "timestamp",
        "path",
        "duration_ms",
        "stt_ms",
        "dispatch_ms",
        "tools_fired",
        "error",
    )
    missing = [name for name in required if name not in data]
    if missing:
        raise ValueError(f"missing field(s): {sorted(missing)}")

    return TelemetryEntry(
        schema_version=data["schema_version"],
        timestamp=data["timestamp"],
        path=data["path"],
        duration_ms=data["duration_ms"],
        stt_ms=data["stt_ms"],
        dispatch_ms=data["dispatch_ms"],
        tools_fired=data["tools_fired"],
        error=data["error"],
    )


def _ensure_lock_file(lock_path: Path) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if not lock_path.exists():
        open(lock_path, "a", encoding="utf-8").close()
    os.chmod(lock_path, 0o600)


def _rotate_locked(data_path: Path) -> None:
    """Rewrite ``data_path`` to the retained subset, dropping corrupt/expired lines.

    Only ever called while the caller already holds the exclusive lock, so it
    does not acquire a lock itself. It is a no-op unless the file has grown
    beyond :data:`ROTATION_LINE_THRESHOLD` lines.
    """

    if not data_path.exists():
        return
    lines = data_path.read_text(encoding="utf-8").splitlines()
    if len(lines) <= ROTATION_LINE_THRESHOLD:
        return

    # Parse each line, in file order; drop any that fail validation.
    valid: list[tuple[int, TelemetryEntry]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            valid.append((index, parse_line(line)))
        except ValueError:
            continue

    # Identify the "most recent N by timestamp" surviving set.
    ordered_by_ts = sorted(valid, key=lambda pair: _parse_timestamp(pair[1].timestamp))
    most_recent_ids = {id(entry) for _, entry in ordered_by_ts[-MAX_RETAINED_ENTRIES:]}

    # Keep only entries that are both in the most-recent-N set and under 30 days old.
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=RETENTION_SECONDS)
    retained = [
        entry
        for _, entry in valid
        if id(entry) in most_recent_ids and _parse_timestamp(entry.timestamp) >= cutoff
    ]

    # Write to a temp file, then atomically replace (same filesystem).
    tmp_path = data_path.parent / (data_path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        for entry in retained:
            handle.write(entry.to_json_line())
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, data_path)


def append_entry(
    entry: TelemetryEntry,
    *,
    data_path=config.TELEMETRY_PATH,
    lock_path=config.TELEMETRY_LOCK_PATH,
) -> None:
    """Append ``entry`` to ``data_path`` under the exclusive lock, then rotate.

    The append and the rotation happen in one locked critical section so a
    concurrent reader can never observe an append without its rotation.
    """

    data_path = Path(data_path)
    lock_path = Path(lock_path)
    _ensure_lock_file(lock_path)
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        created = not data_path.exists()
        with open(data_path, "a", encoding="utf-8") as handle:
            handle.write(entry.to_json_line())
            handle.flush()
            os.fsync(handle.fileno())
        if created:
            os.chmod(data_path, 0o600)
        _rotate_locked(data_path)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_recent(
    limit: int,
    *,
    data_path=config.TELEMETRY_PATH,
    lock_path=config.TELEMETRY_LOCK_PATH,
) -> list[dict]:
    """Return the last ``limit`` valid entries (chronological) as plain dicts."""

    data_path = Path(data_path)
    if not data_path.exists():
        return []

    lock_path = Path(lock_path)
    _ensure_lock_file(lock_path)
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        text = data_path.read_text(encoding="utf-8")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    entries: list[TelemetryEntry] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entries.append(parse_line(line))
        except ValueError:
            continue  # tolerate a corrupt tail; keep the valid entries.

    entries = entries[-limit:]
    return [dataclasses.asdict(entry) for entry in entries]
