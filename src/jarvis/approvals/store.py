"""Single-slot pending-approval store shared between the API process and the
voice-loop process, guarded by fcntl.flock (same discipline as
telemetry/store.py). orchestrator/permissions.py's confirmation gate uses this
so a pending vault-write confirmation can be answered from either an attached
terminal or the HUD, whichever responds first.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis import config


@dataclass
class PendingApproval:
    id: str
    tool_name: str
    description: str
    requested_at: str
    expires_at: str
    decision: str | None = None
    decided_by: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_lock_file(lock_path: Path) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if not lock_path.exists():
        open(lock_path, "a", encoding="utf-8").close()
    os.chmod(lock_path, 0o600)


def _read_locked(data_path: Path) -> PendingApproval | None:
    if not data_path.exists():
        return None
    try:
        raw = json.loads(data_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    try:
        return PendingApproval(**raw)
    except TypeError:
        return None


def _write_locked(data_path: Path, record: PendingApproval) -> None:
    with open(data_path, "w", encoding="utf-8") as handle:
        json.dump(dataclasses.asdict(record), handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(data_path, 0o600)


def _delete_locked(data_path: Path) -> None:
    if data_path.exists():
        data_path.unlink()


def _is_expired(record: PendingApproval) -> bool:
    return datetime.fromisoformat(record.expires_at) <= _now()


def claim(
    tool_name: str,
    description: str,
    *,
    timeout_seconds: float,
    data_path=config.PENDING_APPROVAL_PATH,
    lock_path=config.PENDING_APPROVAL_LOCK_PATH,
) -> PendingApproval | None:
    """Create and store a new pending-approval record, unless one already exists
    and hasn't expired yet — in which case this returns None without writing,
    so the caller knows the HUD channel is not available for this call."""

    data_path = Path(data_path)
    lock_path = Path(lock_path)
    _ensure_lock_file(lock_path)
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        existing = _read_locked(data_path)
        if existing is not None and not _is_expired(existing):
            return None
        now = _now()
        record = PendingApproval(
            id=uuid.uuid4().hex,
            tool_name=tool_name,
            description=description,
            requested_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=timeout_seconds)).isoformat(),
        )
        _write_locked(data_path, record)
        return record
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def decide(
    pending_id: str,
    decision: str,
    *,
    decided_by: str,
    data_path=config.PENDING_APPROVAL_PATH,
    lock_path=config.PENDING_APPROVAL_LOCK_PATH,
) -> bool:
    """Write a decision into the existing record if its id matches, it hasn't
    expired, and it hasn't already been decided. Returns False (no-op) in
    every other case — this is what a HUD's POST /allow or /deny turns into a
    409 when the id is stale or the record is gone."""

    if decision not in ("allow", "deny"):
        raise ValueError(f"decision must be 'allow' or 'deny', got {decision!r}")
    data_path = Path(data_path)
    lock_path = Path(lock_path)
    _ensure_lock_file(lock_path)
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        existing = _read_locked(data_path)
        if existing is None or existing.id != pending_id or _is_expired(existing):
            return False
        if existing.decision is not None:
            return False
        existing.decision = decision
        existing.decided_by = decided_by
        _write_locked(data_path, existing)
        return True
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def clear(
    pending_id: str,
    *,
    data_path=config.PENDING_APPROVAL_PATH,
    lock_path=config.PENDING_APPROVAL_LOCK_PATH,
) -> None:
    """Delete the record if its id matches; a no-op otherwise (already cleared
    by someone else, or never existed). Idempotent by design — callers are not
    expected to check a return value."""

    data_path = Path(data_path)
    lock_path = Path(lock_path)
    _ensure_lock_file(lock_path)
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        existing = _read_locked(data_path)
        if existing is not None and existing.id == pending_id:
            _delete_locked(data_path)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def peek(
    *,
    data_path=config.PENDING_APPROVAL_PATH,
    lock_path=config.PENDING_APPROVAL_LOCK_PATH,
) -> PendingApproval | None:
    """Return the current pending record (decided or not), or None if there
    isn't one or it has expired. An expired record found here is
    opportunistically deleted, same cleanup-on-access pattern as telemetry
    rotation — not left as permanent dead state."""

    data_path = Path(data_path)
    if not data_path.exists():
        return None
    lock_path = Path(lock_path)
    _ensure_lock_file(lock_path)
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        record = _read_locked(data_path)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    if record is None:
        return None
    if not _is_expired(record):
        return record

    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        current = _read_locked(data_path)
        if current is not None and current.id == record.id:
            _delete_locked(data_path)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return None
