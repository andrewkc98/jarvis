"""Secure, owner-scoped runtime status shared by Jarvis processes.

The status file intentionally contains only a small allowlisted schema.  It is
used as a coordination signal for the HUD, not as a diagnostic or conversation
log: prompts, responses, exception messages, and arbitrary caller content never
enter the file.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import math
import os
import tempfile
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psutil

from jarvis import config

SCHEMA_VERSION = 1
ERROR_TTL_SECONDS = 8
_FILE_MODE = 0o600
_MAX_ID_LENGTH = 128

ACTIVE_STATES = frozenset({"listening", "processing", "speaking"})
SOURCES = frozenset({"api", "cli"})
ERROR_CODES = frozenset(
    {
        "capture_failed",
        "stt_failed",
        "routing_failed",
        "schedule_failed",
        "sdk_failed",
        "tts_failed",
        "service_unavailable",
    }
)

RUNTIME_STATUS_PATH = config.RUNTIME_STATUS_PATH
RUNTIME_STATUS_LOCK_PATH = config.RUNTIME_STATUS_LOCK_PATH


@dataclasses.dataclass(frozen=True)
class Publisher:
    """Identity for one process's slot in the shared status file."""

    owner_id: str
    source: str
    pid: int
    process_create_time: float


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _default_process_probe(pid: int) -> float:
    return psutil.Process(pid).create_time()


def _clock_value(clock: Callable[[], datetime] | None) -> datetime:
    value = (clock or _now)()
    if not isinstance(value, datetime):
        raise ValueError("clock must return a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return an aware datetime")
    return value


def _path(value: str | os.PathLike[str] | None, default: Path) -> Path:
    return Path(default if value is None else value)


def _paths(
    data_path: str | os.PathLike[str] | None,
    lock_path: str | os.PathLike[str] | None,
) -> tuple[Path, Path]:
    return _path(data_path, RUNTIME_STATUS_PATH), _path(
        lock_path, RUNTIME_STATUS_LOCK_PATH
    )


def _require_nonempty_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_ID_LENGTH:
        raise ValueError(f"{name} must be a non-empty bounded string")
    return value


def _require_source(source: Any) -> str:
    if not isinstance(source, str) or source not in SOURCES:
        raise ValueError("source must be 'api' or 'cli'")
    return source


def _require_state(state: Any) -> str:
    if not isinstance(state, str) or state not in ACTIVE_STATES:
        raise ValueError("state is not active")
    return state


def _require_code(code: Any) -> str:
    if not isinstance(code, str) or code not in ERROR_CODES:
        raise ValueError("error code is not allowlisted")
    return code


def _parse_aware_timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed


def _validate_pid(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("pid must be a positive integer")
    return value


def _validate_creation_time(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("process_create_time must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        raise ValueError("process_create_time must be finite and positive")
    return numeric


def _validate_publisher(owner: Publisher) -> Publisher:
    if not isinstance(owner, Publisher):
        raise ValueError("owner must be a Publisher")
    _require_nonempty_id(owner.owner_id, "owner_id")
    _require_source(owner.source)
    _validate_pid(owner.pid)
    _validate_creation_time(owner.process_create_time)
    return owner


def _matches_owner(instance: dict[str, Any], owner: Publisher) -> bool:
    return (
        instance.get("pid") == owner.pid
        and instance.get("process_create_time") == owner.process_create_time
        and instance.get("source") == owner.source
    )


def _empty_root() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "instances": {}, "last_error": None}


def _validate_error(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("last_error must be an object or null")
    required = {
        "owner_id",
        "turn_id",
        "source",
        "code",
        "occurred_at",
        "expires_at",
    }
    if set(raw) != required:
        raise ValueError("last_error fields are invalid")
    owner_id = raw["owner_id"]
    turn_id = raw["turn_id"]
    if (owner_id is None) != (turn_id is None):
        raise ValueError("last_error owner_id and turn_id must be paired")
    if owner_id is not None:
        owner_id = _require_nonempty_id(owner_id, "owner_id")
    if turn_id is not None:
        turn_id = _require_nonempty_id(turn_id, "turn_id")
    source = _require_source(raw["source"])
    code = _require_code(raw["code"])
    occurred_at = _parse_aware_timestamp(raw["occurred_at"], "occurred_at")
    expires_at = _parse_aware_timestamp(raw["expires_at"], "expires_at")
    if expires_at != occurred_at + timedelta(seconds=ERROR_TTL_SECONDS):
        raise ValueError("last_error expiry must be exactly eight seconds")
    return {
        "owner_id": owner_id,
        "turn_id": turn_id,
        "source": source,
        "code": code,
        "occurred_at": raw["occurred_at"],
        "expires_at": raw["expires_at"],
    }


def _validate_instance(owner_id: Any, raw: Any) -> dict[str, Any]:
    _require_nonempty_id(owner_id, "owner_id")
    if not isinstance(raw, dict):
        raise ValueError("instance must be an object")
    required = {
        "pid",
        "process_create_time",
        "source",
        "state",
        "turn_id",
        "updated_at",
    }
    if set(raw) != required:
        raise ValueError("instance fields are invalid")
    return {
        "pid": _validate_pid(raw["pid"]),
        "process_create_time": _validate_creation_time(raw["process_create_time"]),
        "source": _require_source(raw["source"]),
        "state": _require_state(raw["state"]),
        "turn_id": _require_nonempty_id(raw["turn_id"], "turn_id"),
        "updated_at": raw["updated_at"],
    }


def _validate_root(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "instances", "last_error"}:
        raise ValueError("root fields are invalid")
    version = raw["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise ValueError("unsupported schema version")
    instances = raw["instances"]
    if not isinstance(instances, dict):
        raise ValueError("instances must be an object")
    validated_instances = {
        owner_id: _validate_instance(owner_id, instance)
        for owner_id, instance in instances.items()
    }
    for instance in validated_instances.values():
        _parse_aware_timestamp(instance["updated_at"], "updated_at")
    return {
        "schema_version": SCHEMA_VERSION,
        "instances": validated_instances,
        "last_error": _validate_error(raw["last_error"]),
    }


def _load_unlocked_result(data_path: Path) -> tuple[dict[str, Any], bool]:
    if not data_path.exists():
        return _empty_root(), True
    try:
        raw = json.loads(data_path.read_text(encoding="utf-8"))
        return _validate_root(raw), True
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError):
        return _empty_root(), False


def _load_unlocked(data_path: Path) -> dict[str, Any]:
    return _load_unlocked_result(data_path)[0]


def _ensure_lock(lock_path: Path) -> None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, _FILE_MODE)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    os.chmod(lock_path, _FILE_MODE)


def _write_unlocked(data_path: Path, root: dict[str, Any]) -> None:
    data_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    fd: int | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{data_path.name}.", dir=data_path.parent
        )
        temporary_path = Path(temporary_name)
        os.fchmod(fd, _FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(root, handle, separators=(",", ":"), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, data_path)
        temporary_path = None
        try:
            directory_fd = os.open(data_path.parent, os.O_RDONLY)
        except OSError:
            pass
        else:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _locked_update(
    data_path: Path,
    lock_path: Path,
    update: Callable[[dict[str, Any]], bool],
) -> None:
    _ensure_lock(lock_path)
    fd = os.open(lock_path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if data_path.exists():
            os.chmod(data_path, _FILE_MODE)
        root = _load_unlocked(data_path)
        if update(root):
            _write_unlocked(data_path, root)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _error_record(
    *,
    owner_id: str | None,
    turn_id: str | None,
    source: str,
    code: str,
    now: datetime,
) -> dict[str, Any]:
    _require_source(source)
    _require_code(code)
    occurred_at = now.isoformat()
    return {
        "owner_id": owner_id,
        "turn_id": turn_id,
        "source": source,
        "code": code,
        "occurred_at": occurred_at,
        "expires_at": (now + timedelta(seconds=ERROR_TTL_SECONDS)).isoformat(),
    }


def publisher(
    source: str,
    *,
    process_probe: Callable[[int], float] | None = None,
    pid: int | None = None,
) -> Publisher:
    """Create an owner identity, probing process creation time exactly once."""

    source = _require_source(source)
    process_id = os.getpid() if pid is None else _validate_pid(pid)
    probe = process_probe or _default_process_probe
    process_create_time = probe(process_id)
    return Publisher(
        owner_id=uuid.uuid4().hex,
        source=source,
        pid=process_id,
        process_create_time=_validate_creation_time(process_create_time),
    )


def publish(
    owner: Publisher,
    state: str,
    turn_id: str,
    *,
    data_path: str | os.PathLike[str] | None = None,
    lock_path: str | os.PathLike[str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Publish one bounded active state in the owner's slot."""

    owner = _validate_publisher(owner)
    state = _require_state(state)
    turn_id = _require_nonempty_id(turn_id, "turn_id")
    now = _clock_value(clock)
    data, lock = _paths(data_path, lock_path)

    def update(root: dict[str, Any]) -> bool:
        root["instances"][owner.owner_id] = {
            "pid": owner.pid,
            "process_create_time": owner.process_create_time,
            "source": owner.source,
            "state": state,
            "turn_id": turn_id,
            "updated_at": now.isoformat(),
        }
        return True

    _locked_update(data, lock, update)


def clear(
    owner: Publisher,
    turn_id: str,
    *,
    data_path: str | os.PathLike[str] | None = None,
    lock_path: str | os.PathLike[str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Clear only the matching owner/turn slot."""

    owner = _validate_publisher(owner)
    turn_id = _require_nonempty_id(turn_id, "turn_id")
    now = _clock_value(clock)
    data, lock = _paths(data_path, lock_path)

    def update(root: dict[str, Any]) -> bool:
        instance = root["instances"].get(owner.owner_id)
        if (
            instance is None
            or not _matches_owner(instance, owner)
            or instance["turn_id"] != turn_id
        ):
            return False
        del root["instances"][owner.owner_id]
        last_error = root["last_error"]
        if last_error is not None:
            occurred_at = _parse_aware_timestamp(last_error["occurred_at"], "occurred_at")
            if (
                last_error["owner_id"] == owner.owner_id
                and last_error["source"] == owner.source
                and occurred_at <= now
            ):
                root["last_error"] = None
        return True

    _locked_update(data, lock, update)


def fail(
    owner: Publisher,
    turn_id: str,
    code: str,
    *,
    data_path: str | os.PathLike[str] | None = None,
    lock_path: str | os.PathLike[str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Finish a matching turn with an allowlisted error code."""

    owner = _validate_publisher(owner)
    turn_id = _require_nonempty_id(turn_id, "turn_id")
    _require_code(code)
    now = _clock_value(clock)
    data, lock = _paths(data_path, lock_path)

    def update(root: dict[str, Any]) -> bool:
        instance = root["instances"].get(owner.owner_id)
        if (
            instance is None
            or not _matches_owner(instance, owner)
            or instance["turn_id"] != turn_id
        ):
            return False
        del root["instances"][owner.owner_id]
        root["last_error"] = _error_record(
            owner_id=owner.owner_id,
            turn_id=turn_id,
            source=owner.source,
            code=code,
            now=now,
        )
        return True

    _locked_update(data, lock, update)


def record_error(
    source: str,
    code: str,
    *,
    data_path: str | os.PathLike[str] | None = None,
    lock_path: str | os.PathLike[str] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Record a safe ownerless error without modifying active instances."""

    source = _require_source(source)
    _require_code(code)
    now = _clock_value(clock)
    data, lock = _paths(data_path, lock_path)

    def update(root: dict[str, Any]) -> bool:
        root["last_error"] = _error_record(
            owner_id=None, turn_id=None, source=source, code=code, now=now
        )
        return True

    _locked_update(data, lock, update)


def _public_snapshot(root: dict[str, Any]) -> dict[str, Any]:
    active: tuple[int, datetime, str, dict[str, Any]] | None = None
    priorities = {"processing": 1, "listening": 2, "speaking": 3}
    for owner_id, instance in root["instances"].items():
        updated_at = _parse_aware_timestamp(instance["updated_at"], "updated_at")
        candidate = (priorities[instance["state"]], updated_at, owner_id, instance)
        if active is None or candidate[:3] > active[:3]:
            active = candidate
    if active is not None:
        instance = active[3]
        return {
            "state": instance["state"],
            "source": instance["source"],
            "updated_at": instance["updated_at"],
            "error_code": None,
        }

    last_error = root["last_error"]
    if last_error is not None:
        return {
            "state": "error",
            "source": last_error["source"],
            "updated_at": last_error["occurred_at"],
            "error_code": last_error["code"],
        }
    return {"state": "idle", "source": None, "updated_at": None, "error_code": None}


def snapshot(
    *,
    data_path: str | os.PathLike[str] | None = None,
    lock_path: str | os.PathLike[str] | None = None,
    clock: Callable[[], datetime] | None = None,
    process_probe: Callable[[int], float] | None = None,
) -> dict[str, Any]:
    """Prune dead owners and return the safe aggregate runtime state."""

    now = _clock_value(clock)
    data, lock = _paths(data_path, lock_path)
    probe = process_probe or _default_process_probe
    _ensure_lock(lock)
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        existed = data.exists()
        if existed:
            os.chmod(data, _FILE_MODE)
        root, valid = _load_unlocked_result(data)
        changed = not valid and existed

        for owner_id, instance in list(root["instances"].items()):
            try:
                current_create_time = _validate_creation_time(probe(instance["pid"]))
            except psutil.NoSuchProcess:
                del root["instances"][owner_id]
                changed = True
            except psutil.AccessDenied:
                continue
            except Exception:
                # Only a confirmed missing process or PID reuse may prune a slot.
                continue
            else:
                if current_create_time != instance["process_create_time"]:
                    del root["instances"][owner_id]
                    changed = True

        last_error = root["last_error"]
        if last_error is not None:
            expires_at = _parse_aware_timestamp(last_error["expires_at"], "expires_at")
            if now >= expires_at:
                root["last_error"] = None
                changed = True

        if changed:
            _write_unlocked(data, root)
        return _public_snapshot(root)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
