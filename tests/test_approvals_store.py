"""Tests for ``jarvis.approvals.store``.

Concurrency tests spawn real OS-level child processes (``multiprocessing.Process``)
so they exercise ``fcntl.flock`` semantics across processes, matching the
discipline in ``test_telemetry_store.py`` (advisory locks are process-scoped,
not thread-scoped, so threads would not exercise them).
"""

from __future__ import annotations

import multiprocessing
import json
import os
import re
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jarvis.approvals import store
from jarvis.approvals.store import (
    PendingApproval,
    claim,
    clear,
    decide,
    peek,
)

_UUID4_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "pending_approval.json"
    lock = tmp_path / "pending_approval.json.lock"
    return data, lock


def _seed(
    data: Path,
    *,
    pending_id: str,
    expires_at: str,
    decision: str | None = None,
    decided_by: str | None = None,
) -> PendingApproval:
    """Write a record directly (no lock helpers) so individual tests can control
    its expiry and decision state precisely."""

    requested_at = _iso(60)
    record = PendingApproval(
        id=pending_id,
        tool_name="vault_write",
        description="append a daily note",
        requested_at=requested_at,
        expires_at=expires_at,
        decision=decision,
        decided_by=decided_by,
    )
    store._write_locked(data, record)
    return record


def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


# ---------------------------------------------------------------------------
# claim()
# ---------------------------------------------------------------------------


def test_claim_fresh_on_empty_store(tmp_path: Path) -> None:
    data, lock = _paths(tmp_path)
    record = claim("vault_write", "append a daily note", timeout_seconds=30, data_path=data, lock_path=lock)
    assert record is not None
    assert _UUID4_HEX_RE.match(record.id)
    assert record.decision is None
    assert record.decided_by is None
    assert record.tool_name == "vault_write"
    assert record.description == "append a daily note"
    assert datetime.fromisoformat(record.expires_at) > _now_dt()


def test_claim_returns_none_when_unexpired_record_exists(tmp_path: Path) -> None:
    data, lock = _paths(tmp_path)
    first = claim("vault_write", "first", timeout_seconds=30, data_path=data, lock_path=lock)
    assert first is not None
    before = data.read_text(encoding="utf-8")

    second = claim("vault_write", "second", timeout_seconds=30, data_path=data, lock_path=lock)
    assert second is None
    # The original record must be unchanged — not overwritten.
    assert data.read_text(encoding="utf-8") == before
    reread = peek(data_path=data, lock_path=lock)
    assert reread is not None
    assert reread.id == first.id
    assert reread.tool_name == "vault_write"


def test_claim_succeeds_again_once_existing_record_expired(tmp_path: Path) -> None:
    data, lock = _paths(tmp_path)
    _seed(data, pending_id=uuid.uuid4().hex, expires_at=_iso(5))  # already in the past
    record = claim("vault_write", "new one", timeout_seconds=30, data_path=data, lock_path=lock)
    assert record is not None
    assert record.tool_name == "vault_write"
    assert datetime.fromisoformat(record.expires_at) > _now_dt()


# ---------------------------------------------------------------------------
# decide()
# ---------------------------------------------------------------------------


def test_decide_updates_matching_undecided_record(tmp_path: Path) -> None:
    data, lock = _paths(tmp_path)
    original = claim("vault_write", "desc", timeout_seconds=30, data_path=data, lock_path=lock)
    assert original is not None
    assert decide(original.id, "allow", decided_by="hud", data_path=data, lock_path=lock) is True
    reread = peek(data_path=data, lock_path=lock)
    assert reread is not None
    assert reread.decision == "allow"
    assert reread.decided_by == "hud"


def test_decide_rejects_wrong_id(tmp_path: Path) -> None:
    data, lock = _paths(tmp_path)
    claim("vault_write", "desc", timeout_seconds=30, data_path=data, lock_path=lock)
    assert decide(uuid.uuid4().hex, "deny", decided_by="hud", data_path=data, lock_path=lock) is False


def test_decide_rejects_missing_file(tmp_path: Path) -> None:
    data = tmp_path / "absent.json"
    lock = tmp_path / "absent.json.lock"
    assert decide("whatever", "allow", decided_by="hud", data_path=data, lock_path=lock) is False


def test_decide_rejects_expired_record(tmp_path: Path) -> None:
    data, lock = _paths(tmp_path)
    rec = _seed(data, pending_id=uuid.uuid4().hex, expires_at=_iso(1))
    assert decide(rec.id, "allow", decided_by="hud", data_path=data, lock_path=lock) is False


def test_decide_rejects_already_decided_record(tmp_path: Path) -> None:
    data, lock = _paths(tmp_path)
    original = claim("vault_write", "desc", timeout_seconds=30, data_path=data, lock_path=lock)
    assert original is not None
    assert decide(original.id, "allow", decided_by="terminal", data_path=data, lock_path=lock) is True
    # Second decision on the same id must be a no-op.
    assert decide(original.id, "deny", decided_by="hud", data_path=data, lock_path=lock) is False
    reread = peek(data_path=data, lock_path=lock)
    assert reread is not None
    assert reread.decision == "allow"
    assert reread.decided_by == "terminal"


def test_decide_rejects_invalid_decision_value(tmp_path: Path) -> None:
    data, lock = _paths(tmp_path)
    claim("vault_write", "desc", timeout_seconds=30, data_path=data, lock_path=lock)
    with pytest.raises(ValueError):
        decide("x", "maybe", decided_by="hud", data_path=data, lock_path=lock)


# ---------------------------------------------------------------------------
# clear()
# ---------------------------------------------------------------------------


def test_clear_deletes_matching_record(tmp_path: Path) -> None:
    data, lock = _paths(tmp_path)
    original = claim("vault_write", "desc", timeout_seconds=30, data_path=data, lock_path=lock)
    assert original is not None and data.exists()
    clear(original.id, data_path=data, lock_path=lock)
    assert not data.exists()


def test_clear_is_noop_for_wrong_id(tmp_path: Path) -> None:
    data, lock = _paths(tmp_path)
    original = claim("vault_write", "desc", timeout_seconds=30, data_path=data, lock_path=lock)
    assert original is not None
    clear(uuid.uuid4().hex, data_path=data, lock_path=lock)
    assert data.exists()
    reread = peek(data_path=data, lock_path=lock)
    assert reread is not None and reread.id == original.id


def test_clear_is_noop_for_missing_file(tmp_path: Path) -> None:
    data = tmp_path / "absent.json"
    lock = tmp_path / "absent.json.lock"
    clear("whatever", data_path=data, lock_path=lock)  # must not raise
    assert not data.exists()


# ---------------------------------------------------------------------------
# peek()
# ---------------------------------------------------------------------------


def test_peek_missing_file_returns_none(tmp_path: Path) -> None:
    data = tmp_path / "absent.json"
    lock = tmp_path / "absent.json.lock"
    assert peek(data_path=data, lock_path=lock) is None


def test_peek_returns_unexpired_record_including_decided(tmp_path: Path) -> None:
    data, lock = _paths(tmp_path)
    original = claim("vault_write", "desc", timeout_seconds=30, data_path=data, lock_path=lock)
    assert original is not None
    assert decide(original.id, "deny", decided_by="hud", data_path=data, lock_path=lock) is True
    reread = peek(data_path=data, lock_path=lock)
    assert reread is not None
    assert reread.id == original.id
    assert reread.decision == "deny"  # peek does not hide a decided record


def test_peek_expired_returns_none_and_deletes_file(tmp_path: Path) -> None:
    data, lock = _paths(tmp_path)
    _seed(data, pending_id=uuid.uuid4().hex, expires_at=_iso(1))
    assert data.exists()
    assert peek(data_path=data, lock_path=lock) is None
    assert not data.exists()


# ---------------------------------------------------------------------------
# File permissions
# ---------------------------------------------------------------------------


def test_claim_sets_0600_on_data_and_lock_files(tmp_path: Path) -> None:
    data, lock = _paths(tmp_path)
    previous_umask = os.umask(0)
    try:
        claim("vault_write", "desc", timeout_seconds=30, data_path=data, lock_path=lock)
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(data.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def _raw_record() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "id": uuid.uuid4().hex,
        "tool_name": "vault_write",
        "description": "append a daily note",
        "requested_at": (now - timedelta(seconds=1)).isoformat(),
        "expires_at": (now + timedelta(seconds=30)).isoformat(),
        "decision": None,
        "decided_by": None,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested_at", "2026-09-03T12:00:00"),
        ("expires_at", "2026-09-03T12:00:00"),
        ("requested_at", "not-a-timestamp"),
        ("expires_at", "not-a-timestamp"),
        ("decision", "maybe"),
        ("decided_by", "browser"),
        ("decision", None),
    ],
)
def test_invalid_record_is_fail_closed(tmp_path: Path, field: str, value: object) -> None:
    data, lock = _paths(tmp_path)
    raw = _raw_record()
    if field == "decision" and value is None:
        raw["decision"] = "allow"
        raw["decided_by"] = None
    else:
        raw[field] = value
    data.write_text(json.dumps(raw), encoding="utf-8")

    assert peek(data_path=data, lock_path=lock) is None
    pending_id = str(raw["id"])
    assert decide(pending_id, "allow", decided_by="hud", data_path=data, lock_path=lock) is False
    clear(pending_id, data_path=data, lock_path=lock)

    replacement = claim("vault_write", "replacement", timeout_seconds=30, data_path=data, lock_path=lock)
    assert replacement is not None


@pytest.mark.parametrize("contents", ["{", "null", "[]"])
def test_corrupt_or_partial_record_is_absent(tmp_path: Path, contents: str) -> None:
    data, lock = _paths(tmp_path)
    data.write_text(contents, encoding="utf-8")
    assert peek(data_path=data, lock_path=lock) is None
    assert decide("anything", "deny", decided_by="terminal", data_path=data, lock_path=lock) is False
    clear("anything", data_path=data, lock_path=lock)


def test_legacy_lock_file_is_repaired_before_use(tmp_path: Path) -> None:
    data, lock = _paths(tmp_path)
    lock.write_bytes(b"")
    lock.chmod(0o666)
    claim("vault_write", "desc", timeout_seconds=30, data_path=data, lock_path=lock)
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_atomic_record_update_uses_same_directory_temporary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data, lock = _paths(tmp_path)
    replacements: list[tuple[Path, Path, int]] = []
    original_replace = os.replace

    def tracking_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        replacements.append((source_path, destination_path, stat.S_IMODE(source_path.stat().st_mode)))
        original_replace(source, destination)

    monkeypatch.setattr(store.os, "replace", tracking_replace)
    original = claim("vault_write", "desc", timeout_seconds=30, data_path=data, lock_path=lock)
    assert original is not None
    assert decide(original.id, "deny", decided_by="terminal", data_path=data, lock_path=lock) is True

    assert len(replacements) == 2
    for source, destination, mode in replacements:
        assert source.parent == data.parent
        assert source.name.startswith(f".{data.name}.")
        assert destination == data
        assert mode == 0o600
        assert not source.exists()
    reread = peek(data_path=data, lock_path=lock)
    assert reread is not None
    assert reread.id == original.id
    assert reread.decision == "deny"
    assert reread.decided_by == "terminal"


# ---------------------------------------------------------------------------
# Cross-process concurrency (real OS processes)
# ---------------------------------------------------------------------------


def _worker_claim(data: str, lock: str, start_barrier, out_queue) -> None:
    # Wait in a barrier so both processes race the same instant.
    start_barrier.wait()
    record = claim(
        "vault_write",
        "concurrent claim",
        timeout_seconds=30,
        data_path=Path(data),
        lock_path=Path(lock),
    )
    out_queue.put(record.id if record is not None else None)
    # After winning, prove the file is fully parseable (no torn read).
    if record is not None:
        reread = peek(data_path=Path(data), lock_path=Path(lock))
        assert reread is not None and reread.id == record.id


def test_concurrent_claim_exactly_one_wins(tmp_path: Path) -> None:
    data, lock = _paths(tmp_path)
    barrier = multiprocessing.Barrier(2)
    q = multiprocessing.Queue()

    procs = [
        multiprocessing.Process(target=_worker_claim, args=(str(data), str(lock), barrier, q))
        for _ in range(2)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join()

    assert all(p.exitcode == 0 for p in procs), "a worker raised (torn/partial file observed)"
    outcomes = [q.get() for _ in range(2)]
    winners = [value for value in outcomes if value is not None]
    assert len(winners) == 1, f"expected exactly one winner, got {outcomes}"
    # The surviving file must be exactly one record.
    reread = peek(data_path=data, lock_path=lock)
    assert reread is not None
    assert reread.id == winners[0]


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)
