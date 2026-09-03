"""Tests for the secure owner-scoped runtime status foundation."""

from __future__ import annotations

import json
import math
import multiprocessing
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jarvis.runtime import status


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "runtime_status.json"
    return data, tmp_path / "runtime_status.json.lock"


def _clock(value: datetime):
    return lambda: value


def _owner(source: str = "cli", owner_id: str = "owner-1") -> status.Publisher:
    return status.Publisher(
        owner_id=owner_id,
        source=source,
        pid=os.getpid(),
        process_create_time=123.5,
    )


def _read_root(data: Path) -> dict:
    return json.loads(data.read_text(encoding="utf-8"))


def test_publisher_captures_pid_and_process_creation_once(monkeypatch):
    calls = []

    def probe(pid):
        calls.append(pid)
        return 123.5

    owner = status.publisher("api", pid=77, process_probe=probe)
    assert owner.source == "api"
    assert owner.pid == 77
    assert owner.process_create_time == 123.5
    assert len(owner.owner_id) == 32
    assert calls == [77]


@pytest.mark.parametrize("source", ["", "hud", None, 1])
def test_invalid_source_rejected(source):
    with pytest.raises(ValueError):
        status.publisher(source, process_probe=lambda _pid: 1.0)


@pytest.mark.parametrize("value", [0, -1, math.inf, math.nan, "123"])
def test_invalid_process_creation_time_rejected(value):
    with pytest.raises(ValueError):
        status.publisher("cli", process_probe=lambda _pid, value=value: value)


def test_publish_creates_exact_secure_root_and_preserves_owner_shape(tmp_path):
    data, lock = _paths(tmp_path)
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    previous_umask = os.umask(0)
    try:
        status.publish(_owner(), "listening", "turn-1", data_path=data, lock_path=lock, clock=_clock(now))
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(data.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    root = _read_root(data)
    assert set(root) == {"schema_version", "instances", "last_error"}
    assert root["schema_version"] == 1
    assert root["last_error"] is None
    assert root["instances"]["owner-1"] == {
        "pid": os.getpid(),
        "process_create_time": 123.5,
        "source": "cli",
        "state": "listening",
        "turn_id": "turn-1",
        "updated_at": now.isoformat(),
    }


def test_existing_data_and_lock_permissions_are_repaired(tmp_path):
    data, lock = _paths(tmp_path)
    data.write_text(json.dumps({"schema_version": 1, "instances": {}, "last_error": None}))
    lock.write_bytes(b"")
    data.chmod(0o666)
    lock.chmod(0o666)

    status.publish(_owner(), "processing", "turn-1", data_path=data, lock_path=lock)

    assert stat.S_IMODE(data.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_publish_isolated_per_owner_and_clear_requires_current_turn(tmp_path):
    data, lock = _paths(tmp_path)
    first = _owner(owner_id="first")
    second = _owner(owner_id="second")
    status.publish(first, "processing", "turn-a", data_path=data, lock_path=lock)
    status.publish(second, "speaking", "turn-b", data_path=data, lock_path=lock)

    status.clear(first, "stale-turn", data_path=data, lock_path=lock)
    root = _read_root(data)
    assert set(root["instances"]) == {"first", "second"}

    status.clear(first, "turn-a", data_path=data, lock_path=lock)
    root = _read_root(data)
    assert set(root["instances"]) == {"second"}


def test_fail_requires_matching_turn_and_only_sets_allowlisted_error(tmp_path):
    data, lock = _paths(tmp_path)
    owner = _owner()
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    status.publish(owner, "processing", "turn-a", data_path=data, lock_path=lock, clock=_clock(now))
    status.fail(owner, "stale-turn", "sdk_failed", data_path=data, lock_path=lock, clock=_clock(now))
    assert "owner-1" in _read_root(data)["instances"]

    status.fail(owner, "turn-a", "sdk_failed", data_path=data, lock_path=lock, clock=_clock(now))
    root = _read_root(data)
    assert root["instances"] == {}
    assert root["last_error"] == {
        "owner_id": "owner-1",
        "turn_id": "turn-a",
        "source": "cli",
        "code": "sdk_failed",
        "occurred_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=8)).isoformat(),
    }


def test_record_error_is_ownerless_and_preserves_active_instances(tmp_path):
    data, lock = _paths(tmp_path)
    owner = _owner()
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    status.publish(owner, "processing", "turn-a", data_path=data, lock_path=lock)
    status.record_error("api", "service_unavailable", data_path=data, lock_path=lock, clock=_clock(now))
    root = _read_root(data)
    assert "owner-1" in root["instances"]
    assert root["last_error"]["owner_id"] is None
    assert root["last_error"]["turn_id"] is None
    assert root["last_error"]["source"] == "api"
    assert root["last_error"]["code"] == "service_unavailable"


@pytest.mark.parametrize("state", ["idle", "error", "", None, 1])
def test_publish_rejects_non_active_state(tmp_path, state):
    data, lock = _paths(tmp_path)
    with pytest.raises(ValueError):
        status.publish(_owner(), state, "turn-a", data_path=data, lock_path=lock)
    assert not data.exists()


@pytest.mark.parametrize("code", ["oops", "ConnectionError", "", None, 1])
def test_error_operations_reject_unallowlisted_codes(tmp_path, code):
    data, lock = _paths(tmp_path)
    with pytest.raises(ValueError):
        status.record_error("cli", code, data_path=data, lock_path=lock)
    assert not data.exists()


@pytest.mark.parametrize(
    "mutator",
    [
        lambda root: root.update({"extra": 1}),
        lambda root: root.update({"schema_version": 1.0}),
        lambda root: root["instances"].update({"x": {"state": "processing"}}),
        lambda root: root.update({"last_error": {"code": "secret"}}),
    ],
)
def test_invalid_root_fails_closed_to_empty_root(tmp_path, mutator):
    data, lock = _paths(tmp_path)
    raw = {"schema_version": 1, "instances": {}, "last_error": None}
    mutator(raw)
    data.write_text(json.dumps(raw), encoding="utf-8")

    status.clear(_owner(), "turn-a", data_path=data, lock_path=lock)
    status.publish(_owner(), "processing", "turn-a", data_path=data, lock_path=lock)
    root = _read_root(data)
    assert set(root) == {"schema_version", "instances", "last_error"}
    assert list(root["instances"]) == ["owner-1"]


def test_timestamps_must_be_aware_and_valid(tmp_path):
    data, lock = _paths(tmp_path)
    raw = {
        "schema_version": 1,
        "instances": {
            "owner-1": {
                "pid": os.getpid(),
                "process_create_time": 1.0,
                "source": "cli",
                "state": "processing",
                "turn_id": "turn-a",
                "updated_at": "2026-09-03T12:00:00",
            }
        },
        "last_error": None,
    }
    data.write_text(json.dumps(raw), encoding="utf-8")
    status.publish(_owner(), "speaking", "turn-b", data_path=data, lock_path=lock)
    root = _read_root(data)
    assert list(root["instances"]) == ["owner-1"]
    assert root["instances"]["owner-1"]["turn_id"] == "turn-b"


def test_clock_is_injected_for_publish_and_error_expiry(tmp_path):
    data, lock = _paths(tmp_path)
    first = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    second = first + timedelta(seconds=2)
    owner = _owner()
    status.publish(owner, "processing", "turn-a", data_path=data, lock_path=lock, clock=_clock(first))
    status.fail(owner, "turn-a", "tts_failed", data_path=data, lock_path=lock, clock=_clock(second))
    root = _read_root(data)
    assert root["last_error"]["occurred_at"] == second.isoformat()
    assert root["last_error"]["expires_at"] == (second + timedelta(seconds=8)).isoformat()


def test_atomic_update_replaces_same_directory_temp(tmp_path, monkeypatch):
    data, lock = _paths(tmp_path)
    replacements = []
    original_replace = os.replace

    def track(source, destination):
        source = Path(source)
        replacements.append((source, Path(destination), stat.S_IMODE(source.stat().st_mode)))
        original_replace(source, destination)

    monkeypatch.setattr(status.os, "replace", track)
    status.publish(_owner(), "processing", "turn-a", data_path=data, lock_path=lock)
    status.publish(_owner(), "speaking", "turn-b", data_path=data, lock_path=lock)
    assert len(replacements) == 2
    for source, destination, mode in replacements:
        assert source.parent == data.parent
        assert source.name.startswith(f".{data.name}.")
        assert destination == data
        assert mode == 0o600
        assert not source.exists()


def test_writer_never_persists_arbitrary_error_content(tmp_path):
    data, lock = _paths(tmp_path)
    with pytest.raises(ValueError):
        status.record_error("cli", "secret message", data_path=data, lock_path=lock)
    assert not data.exists()


@pytest.mark.parametrize(
    ("owner_id", "turn_id"),
    [("owner-1", None), (None, "turn-a")],
)
def test_snapshot_rejects_unpaired_error_ownership_fields(tmp_path, owner_id, turn_id):
    data, lock = _paths(tmp_path)
    raw = {
        "schema_version": 1,
        "instances": {},
        "last_error": {
            "owner_id": owner_id,
            "turn_id": turn_id,
            "source": "cli",
            "code": "sdk_failed",
            "occurred_at": "2026-09-03T12:00:00+00:00",
            "expires_at": "2026-09-03T12:00:08+00:00",
        },
    }
    data.write_text(json.dumps(raw), encoding="utf-8")

    assert status.snapshot(data_path=data, lock_path=lock) == {
        "state": "idle",
        "source": None,
        "updated_at": None,
        "error_code": None,
    }
    assert json.loads(data.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "instances": {},
        "last_error": None,
    }


def test_snapshot_idle_when_store_is_missing(tmp_path):
    data, lock = _paths(tmp_path)
    assert status.snapshot(data_path=data, lock_path=lock) == {
        "state": "idle",
        "source": None,
        "updated_at": None,
        "error_code": None,
    }
    assert lock.exists()


def test_snapshot_prunes_dead_pid_and_rewrites_root(tmp_path):
    data, lock = _paths(tmp_path)
    owner = _owner()
    status.publish(owner, "processing", "turn-a", data_path=data, lock_path=lock)

    def probe(_pid):
        raise status.psutil.NoSuchProcess(owner.pid)

    assert status.snapshot(data_path=data, lock_path=lock, process_probe=probe) == {
        "state": "idle",
        "source": None,
        "updated_at": None,
        "error_code": None,
    }
    assert _read_root(data)["instances"] == {}


def test_snapshot_prunes_pid_reuse(tmp_path):
    data, lock = _paths(tmp_path)
    owner = _owner()
    status.publish(owner, "processing", "turn-a", data_path=data, lock_path=lock)
    assert status.snapshot(
        data_path=data, lock_path=lock, process_probe=lambda _pid: 999.0
    )["state"] == "idle"
    assert _read_root(data)["instances"] == {}


def test_snapshot_preserves_access_denied_process(tmp_path):
    data, lock = _paths(tmp_path)
    owner = _owner()
    status.publish(owner, "processing", "turn-a", data_path=data, lock_path=lock)

    def probe(_pid):
        raise status.psutil.AccessDenied(owner.pid)

    result = status.snapshot(data_path=data, lock_path=lock, process_probe=probe)
    assert result["state"] == "processing"
    assert "owner-1" in _read_root(data)["instances"]


def test_snapshot_expires_last_error_at_eight_seconds(tmp_path):
    data, lock = _paths(tmp_path)
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    status.record_error("api", "service_unavailable", data_path=data, lock_path=lock, clock=_clock(now))
    assert status.snapshot(data_path=data, lock_path=lock, clock=_clock(now + timedelta(seconds=7)))["state"] == "error"
    assert status.snapshot(data_path=data, lock_path=lock, clock=_clock(now + timedelta(seconds=8))) == {
        "state": "idle",
        "source": None,
        "updated_at": None,
        "error_code": None,
    }
    assert _read_root(data)["last_error"] is None


def test_snapshot_active_priority_and_newest_tie_breaking(tmp_path):
    data, lock = _paths(tmp_path)
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    processing = _owner("cli", "processing-owner")
    listening = _owner("api", "listening-owner")
    speaking_old = _owner("cli", "speaking-old")
    speaking_new = _owner("api", "speaking-new")
    for owner, state, when in (
        (processing, "processing", now + timedelta(seconds=4)),
        (listening, "listening", now + timedelta(seconds=3)),
        (speaking_old, "speaking", now + timedelta(seconds=1)),
        (speaking_new, "speaking", now + timedelta(seconds=2)),
    ):
        status.publish(owner, state, owner.owner_id, data_path=data, lock_path=lock, clock=_clock(when))

    result = status.snapshot(
        data_path=data,
        lock_path=lock,
        clock=_clock(now + timedelta(seconds=5)),
        process_probe=lambda _pid: 123.5,
    )
    assert result == {
        "state": "speaking",
        "source": "api",
        "updated_at": (now + timedelta(seconds=2)).isoformat(),
        "error_code": None,
    }
    assert set(result) == {"state", "source", "updated_at", "error_code"}
    assert "pid" not in result
    assert "owner_id" not in result


def test_snapshot_active_state_outranks_error(tmp_path):
    data, lock = _paths(tmp_path)
    owner = _owner()
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    status.record_error("api", "sdk_failed", data_path=data, lock_path=lock, clock=_clock(now))
    status.publish(owner, "processing", "turn-a", data_path=data, lock_path=lock, clock=_clock(now))
    result = status.snapshot(data_path=data, lock_path=lock, clock=_clock(now), process_probe=lambda _pid: 123.5)
    assert result["state"] == "processing"
    assert result["error_code"] is None


def test_snapshot_corrupt_root_fails_closed_and_replaces_it(tmp_path):
    data, lock = _paths(tmp_path)
    data.write_text("{not-json", encoding="utf-8")
    assert status.snapshot(data_path=data, lock_path=lock) == {
        "state": "idle",
        "source": None,
        "updated_at": None,
        "error_code": None,
    }
    assert _read_root(data) == {"schema_version": 1, "instances": {}, "last_error": None}


def test_clear_only_clears_older_matching_owner_error(tmp_path):
    data, lock = _paths(tmp_path)
    owner = _owner()
    first = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    second = first + timedelta(seconds=1)
    status.publish(owner, "processing", "turn-a", data_path=data, lock_path=lock, clock=_clock(first))
    status.fail(owner, "turn-a", "sdk_failed", data_path=data, lock_path=lock, clock=_clock(first))
    status.publish(owner, "processing", "turn-b", data_path=data, lock_path=lock, clock=_clock(second))
    status.clear(owner, "turn-b", data_path=data, lock_path=lock, clock=_clock(second))
    assert _read_root(data)["last_error"] is None


def _worker_publish_clear(data: str, lock: str, barrier, result_queue) -> None:
    owner = status.publisher("cli")
    status.publish(owner, "processing", owner.owner_id, data_path=data, lock_path=lock)
    result_queue.put(owner.owner_id)
    barrier.wait()
    barrier.wait()
    status.clear(owner, owner.owner_id, data_path=data, lock_path=lock)


def test_concurrent_publish_and_clear_preserve_owner_slots(tmp_path):
    data, lock = _paths(tmp_path)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    result_queue = context.Queue()
    workers = [
        context.Process(
            target=_worker_publish_clear,
            args=(str(data), str(lock), barrier, result_queue),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    owner_ids = {result_queue.get(timeout=10) for _ in workers}
    barrier.wait()
    snapshot = status.snapshot(data_path=data, lock_path=lock)
    root = _read_root(data)
    assert set(root["instances"]) == owner_ids
    assert snapshot["state"] == "processing"
    barrier.wait()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0
    assert status.snapshot(data_path=data, lock_path=lock)["state"] == "idle"
